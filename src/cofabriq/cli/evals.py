"""`cofabriq eval ...`: freeze the eval sets and score them."""

import json as json_mod
import time
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from statistics import fmean, median

import click
import httpx
import typer
from pydantic import TypeAdapter, ValidationError

from cofabriq.cli.common import (
    claude_code,
    eval_app,
    github,
    loop_rules,
    openai_key,
    pick_only,
    target_repo,
    triage_fn,
    write_token_for,
)
from cofabriq.config import get_settings
from cofabriq.context.store import postgres
from cofabriq.eval import dedupe
from cofabriq.eval import features as features_eval
from cofabriq.eval import gate as gate_eval
from cofabriq.eval import human as human_eval
from cofabriq.eval import impact as impact_eval
from cofabriq.eval import loop as loop_eval
from cofabriq.eval import readiness as readiness_eval
from cofabriq.eval import refine as refine_eval
from cofabriq.eval import review as review_eval
from cofabriq.eval import triage as triage_eval
from cofabriq.eval import witness as witness_eval
from cofabriq.eval.bugs import PlantedBug, fix_path, load_bugs, prove_bugs, repro_path
from cofabriq.files import write_atomic, write_stamped
from cofabriq.git import files_at
from cofabriq.issues import rerank as rerank_mod
from cofabriq.issues import triage as triage_mod
from cofabriq.issues import vectors
from cofabriq.issues import verdict as verdict_mod
from cofabriq.issues.fetch import load_issues
from cofabriq.issues.model import Issue, check_repo
from cofabriq.loop import publish as publish_mod
from cofabriq.loop import record as record_mod
from cofabriq.loop import refine as refine_mod
from cofabriq.loop import run as run_mod
from cofabriq.loop import sentinel as sentinel_mod
from cofabriq.loop import worktree as worktree_mod
from cofabriq.loop.engine import AgentError, AgentRun, Guard, Guarded
from cofabriq.loop.sandbox import Podman
from cofabriq.map import build as map_build_mod
from cofabriq.map import components as components_mod
from cofabriq.map import fixes as fixes_mod
from cofabriq.map import history
from cofabriq.map import impact as impact_mod
from cofabriq.targets import PLAYGROUND, clone_dir, repo_slug

EVALS_DIR = Path("evals")


def _evalset_path(repo: str) -> Path:
    return EVALS_DIR / f"dedupe-{repo_slug(repo)}.json"


@eval_app.command("dedupe-set")
def dedupe_set(repo: str | None = None, force: bool = False) -> None:
    """Build and freeze the duplicate eval set from the fetched issues."""
    target = target_repo(repo)
    path = _evalset_path(target)
    if path.exists() and not force:
        typer.echo(f"{path} is frozen; --force to rebuild it", err=True)
        raise typer.Exit(code=1)
    settings = get_settings()
    issues = load_issues(settings.issues_dir, target)
    rules = dedupe.load_rules(settings.targets_dir, target)
    evalset, report = dedupe.build(issues, frozen_at=datetime.now(UTC), rules=rules)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(evalset.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(
        f"{report.pairs} pairs ({report.by_signal}) of {report.found} found, "
        f"{len(evalset.negatives)} negatives of {report.negative_pool}, from "
        f"{len(issues)} issues; dropped {report.ambiguous} ambiguous, "
        f"{report.unusable} unusable -> {path}"
    )


@eval_app.command("dedupe")
def eval_dedupe(
    repo: str | None = None, verdict: bool = False, rerank: bool = False
) -> None:
    """Retrieval recall@k and the embedding-threshold baseline on the frozen set.

    `--rerank` adds an LLM rerank of the vector top 50 and scores its top 10
    (paid calls, cached in the rerank dir). `--verdict` adds the LLM duplicate
    verdict over the top 10, reranked with `--rerank` (paid calls, cached in the
    verdicts dir: a rerun costs nothing).
    """
    settings = get_settings()
    target = target_repo(repo)
    evalset = dedupe.EvalSet.model_validate_json(_evalset_path(target).read_bytes())
    issues = {i.number: i for i in load_issues(settings.issues_dir, target)}
    depth = rerank_mod.WIDE if rerank else triage_mod.CANDIDATE_ISSUES
    with postgres.connect(settings.postgres_dsn) as conn:

        def rank(number: int) -> list[tuple[int, float]]:
            before = issues[number].created_at
            hits = vectors.nearest(conn, target, number, depth, before)
            return [(h.number, h.score) for h in hits]

        ranked = dedupe.rank_all(evalset, rank)
    typer.echo(
        f"{target}, frozen {evalset.frozen_at:%Y-%m-%d}: "
        f"{len(evalset.pairs)} pairs, {len(evalset.negatives)} negatives"
    )
    reranked = _reranked(target, ranked, issues) if rerank else None
    shortlist = [
        replace(r, candidates=r.candidates[: triage_mod.CANDIDATE_ISSUES])
        for r in (reranked or ranked)
    ]
    records = _verdicts(target, shortlist, issues) if verdict else None
    for name, cluster in [("strict", False), ("cluster", True)]:
        m = dedupe.score(ranked, cluster=cluster)
        recall = "  ".join(f"@{k} {r:.3f}" for k, r in m.recall_at.items())
        b = m.best
        typer.echo(
            f"  {name:<8} recall {recall}  |  threshold baseline t={b.value:.3f}: "
            f"precision {b.precision:.3f}, recall {b.recall:.3f}, F1 {b.f1:.3f}"
        )
        if reranked is not None:
            wide = dedupe.score(ranked, ks=(20, rerank_mod.WIDE), cluster=cluster)
            after = dedupe.score(reranked, cluster=cluster)
            typer.echo(
                f"  {'':<8} reranked top {rerank_mod.WIDE}: recall "
                + "  ".join(f"@{k} {r:.3f}" for k, r in after.recall_at.items())
                + "  |  vector "
                + "  ".join(f"@{k} {r:.3f}" for k, r in wide.recall_at.items())
            )
        if records is not None:
            answers = {n: (r.verdict, r.target) for n, r in records.items()}
            v = dedupe.score_verdicts(shortlist, answers, cluster=cluster)
            typer.echo(
                f"  {'':<8} LLM verdict ({v.predicted} duplicate_of): "
                f"precision {v.precision:.3f}, recall {v.recall:.3f}, F1 {v.f1:.3f}"
                f"; {v.unsure} unsure, {v.unsure_right} of them right"
            )
    if records is not None:
        rows = list(records.values())
        dropped = sum(bool(r.dropped) for r in rows)
        typer.echo(
            f"  per issue: ${fmean(r.cost_usd for r in rows):.5f} mean, "
            f"{median(r.latency_ms for r in rows) / 1000:.1f} s median latency; "
            f"{dropped} of {len(rows)} verdicts failed the check"
        )


def _infoset_path(repo: str, test: bool) -> Path:
    suffix = "-test" if test else ""
    return EVALS_DIR / f"readiness-{repo_slug(repo)}{suffix}.json"


@eval_app.command("readiness-set")
def readiness_set(
    repo: str | None = None, test: bool = False, force: bool = False
) -> None:
    """Build and freeze the missing-info eval set: maintainer labels from the
    fetched issues, each sampled issue's history checked on GitHub. `--test`
    freezes the held-out split (`test_window`) instead."""
    settings = get_settings()
    target = target_repo(repo)
    path = _infoset_path(target, test)
    if path.exists() and not force:
        typer.echo(f"{path} is frozen; --force to rebuild it", err=True)
        raise typer.Exit(code=1)
    rules = readiness_eval.load_rules(settings.targets_dir, target)
    source = github(target)
    if rules is None or source is None:
        typer.echo("needs [readiness] in the target file and GITHUB_TOKEN", err=True)
        raise typer.Exit(code=1)
    if test:
        if rules.test_window is None:
            typer.echo("no test_window in [readiness]", err=True)
            raise typer.Exit(code=1)
        rules = rules.model_copy(update={"window": rules.test_window})
    pool = readiness_eval.pools(load_issues(settings.issues_dir, target), rules)
    infoset = readiness_eval.build(
        target, pool, rules, source.history, frozen_at=datetime.now(UTC)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(infoset.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(
        f"{len(infoset.needs_info)} needs-info of {len(pool.needs_info)}, "
        f"{len(infoset.complete)} complete of {len(pool.complete)} -> {path}"
    )


@eval_app.command("readiness")
def eval_readiness(repo: str | None = None, test: bool = False) -> None:
    """Does the card ask for more info exactly when a maintainer did? The
    checked required items vs the model's own questions, on the frozen set.

    Paid: one card call per issue, cached in the readiness dir. `--test`
    scores the held-out split: once, after the method is chosen.
    """
    settings = get_settings()
    target = target_repo(repo)
    infoset = readiness_eval.InfoSet.model_validate_json(
        _infoset_path(target, test).read_bytes()
    )
    wanted = set(infoset.needs_info) | set(infoset.complete)
    issues = {
        i.number: i
        for i in load_issues(settings.issues_dir, target)
        if i.number in wanted
    }
    path = settings.readiness_dir / f"{repo_slug(target)}.json"
    triager = triage_mod.OpenAITriager(openai_key())
    records = readiness_eval.run_fields(list(issues.values()), triager, path)
    checked = {
        n: bool(readiness_eval.missing(r, issues[n])) for n, r in records.items()
    }
    asked = {n: bool(r.fields.missing_info) for n, r in records.items()}
    blocking = {
        n: r.fields.blocking and bool(r.fields.missing_info) for n, r in records.items()
    }
    card = {n: checked[n] or blocking[n] for n in records}
    typer.echo(
        f"{target}, frozen {infoset.frozen_at:%Y-%m-%d}: "
        f"{len(infoset.needs_info)} needs-info, {len(infoset.complete)} complete"
    )
    methods = [
        ("model asks anything", asked),
        ("checked items", checked),
        ("blocking questions", blocking),
        ("card (either)", card),
    ]
    for name, asks in methods:
        s = readiness_eval.score(infoset, asks)
        typer.echo(
            f"  {name:<20} needs_info on {s.predicted}: precision "
            f"{s.precision:.3f}, recall {s.recall:.3f}, F1 {s.f1:.3f}"
        )
    rows = list(records.values())
    types = Counter(r.fields.type for r in rows)
    typer.echo(f"  types: {dict(types.most_common())}")
    typer.echo(
        f"  per issue: ${fmean(r.cost_usd for r in rows):.5f} mean, "
        f"{median(r.latency_ms for r in rows) / 1000:.1f} s median latency"
    )


def _impact_path(repo: str, test: bool) -> Path:
    suffix = "-test" if test else ""
    return EVALS_DIR / f"impact-{repo_slug(repo)}{suffix}.json"


@eval_app.command("impact-set")
def impact_set(
    repo: str | None = None, test: bool = False, force: bool = False
) -> None:
    """Freeze the impact eval set: issues closed by a PR or commit, and the
    components their fix commits touched (from the clone in data/clones/).
    `--test` freezes the held-out split."""
    settings = get_settings()
    target = target_repo(repo)
    path = _impact_path(target, test)
    if path.exists() and not force:
        typer.echo(f"{path} is frozen; --force to rebuild it", err=True)
        raise typer.Exit(code=1)
    rules = impact_eval.load_rules(settings.targets_dir, target)
    if rules is None:
        typer.echo("needs [impact] in the target file", err=True)
        raise typer.Exit(code=1)
    clone = clone_dir(settings.clones_dir, target)
    fixed = fixes_mod.fixes(clone, history.rev_at(clone, None), target)
    impact, pool = impact_eval.build(
        load_issues(settings.issues_dir, target),
        fixed,
        components_mod.load_rules(settings.targets_dir, target),
        rules,
        rules.test_window if test else rules.window,
        frozen_at=datetime.now(UTC),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(impact.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(f"{len(impact.cases)} cases of {pool} -> {path}")


@eval_app.command("impact")
def eval_impact(repo: str | None = None, test: bool = False) -> None:
    """Which components will the fix touch, from the issue text alone?
    Popularity vs kNN over earlier fixed issues vs kNN plus co-change, as
    hit@k and recall@k. Free: embeddings and the clone, no LLM. The map is
    built in memory at the rules' `map_until`, so the Neo4j map can stay
    current for `impact`."""
    settings = get_settings()
    target = target_repo(repo)
    impact = impact_eval.ImpactSet.model_validate_json(
        _impact_path(target, test).read_bytes()
    )
    until = impact.rules.map_until.isoformat()
    components = components_mod.load_rules(settings.targets_dir, target)
    clone = clone_dir(settings.clones_dir, target)
    cut_rev = history.rev_at(clone, impact.rules.map_until)
    repo_map = map_build_mod.build(
        files_at(clone, cut_rev), history.commits(clone, cut_rev), components
    )
    popular = repo_map.popular()
    fixed = fixes_mod.fixes(clone, history.rev_at(clone, None), target)
    issues = {i.number: i for i in load_issues(settings.issues_dir, target)}
    cache: dict[str, list[tuple[str, float]]] = {}

    def partners(component: str) -> list[tuple[str, float]]:
        if component not in cache:
            cache[component] = repo_map.partners(component, 10)
        return cache[component]

    methods: dict[str, dict[int, list[str]]] = {
        "popularity": {},
        "kNN": {},
        "kNN + co-change": {},
        "kNN fixed": {},
        "kNN fixed + co-ch.": {},
    }
    empty = {"kNN": 0, "kNN fixed": 0}  # no neighbour had a fix: popularity only
    first_fix = {n: min(c.at for c in commits) for n, commits in fixed.items()}
    with postgres.connect(settings.postgres_dsn) as conn:
        for case in impact.cases:
            issue = issues[case.issue]
            filed = issue.created_at
            known = [n for n, at in first_fix.items() if at < filed]
            for name, among in [("kNN", None), ("kNN fixed", known)]:
                hits = vectors.nearest(
                    conn, target, issue.number, impact_mod.NEIGHBOURS, filed, among
                )
                ranked = impact_eval.knn(
                    [(h.number, h.score) for h in hits], filed, fixed, components
                )
                empty[name] += not ranked
                spread = impact_mod.spread(ranked, partners)
                with_co = "kNN + co-change" if among is None else "kNN fixed + co-ch."
                methods[name][case.issue] = impact_eval.fill(ranked, popular)
                methods[with_co][case.issue] = impact_eval.fill(spread, popular)
            methods["popularity"][case.issue] = popular
    sizes = [len(c.components) for c in impact.cases]
    start, end = impact.window
    typer.echo(
        f"{target} {start}..{end}, map cut {until}: {len(impact.cases)} issues, "
        f"{fmean(sizes):.1f} components touched on average, "
        f"{len(popular)} in the map"
    )
    ks = (1, 3, 5, 10)
    for name, predicted in methods.items():
        s = impact_eval.score(impact.cases, predicted, ks)
        hit = "  ".join(f"@{k} {v:.3f}" for k, v in s.hit_at.items())
        recall = "  ".join(f"@{k} {v:.3f}" for k, v in s.recall_at.items())
        typer.echo(f"  {name:<18} hit {hit}  |  recall {recall}")
    typer.echo(
        "  no evidence (popularity only): "
        + ", ".join(f"{name} {n}/{len(impact.cases)}" for name, n in empty.items())
    )


@eval_app.command("triage")
def eval_triage(
    repo: str | None = None, seed: Path = Path("playground/seed.toml")
) -> None:
    """Type, component, duplicate and injection checks on the seeded issues.

    Paid: two LLM calls per seeded issue, nothing cached. Needs the seeded
    issues fetched and embedded, and the repo's docs embedded.
    """
    settings = get_settings()
    target = target_repo(repo)
    items = triage_eval.load_seed(seed)
    matched = triage_eval.match(items, load_issues(settings.issues_dir, target))
    with postgres.connect(settings.postgres_dsn) as conn:
        conn.autocommit = True  # one connection shared by the threads, reads only
        run = partial(triage_fn(conn, refresh=False), target)
        with ThreadPoolExecutor(max_workers=4) as pool:
            found = pool.map(run, [i.number for i in matched.values()])
            cards = dict(zip(matched, found, strict=True))
    numbers = {k: i.number for k, i in matched.items()}
    s = triage_eval.score(items, cards, numbers)
    runs = [c.run for c in cards.values()]
    typer.echo(f"{target}: {s.n} seeded issues ({seed})")
    typer.echo(f"  type       {s.type_ok}/{s.n}")
    typer.echo(f"  component  {s.component_ok}/{s.n}")
    typer.echo(
        f"  duplicate  {s.duplicates_found}/{s.duplicates} found, "
        f"{s.false_duplicates} false duplicate_of"
    )
    typer.echo(f"  injections followed {s.injections_followed}/{s.injections}")
    typer.echo(
        f"  conflicts  {s.conflicts} card-verdict, "
        f"{s.conflicts_on_type_miss} on a wrong type"
    )
    typer.echo(
        f"  per issue: ${fmean(r.cost_usd for r in runs):.5f} mean, "
        f"{median(r.latency_ms for r in runs) / 1000:.1f} s median latency"
    )
    for miss in s.misses:
        typer.echo(f"  miss  {miss}")


def _reranked(
    repo: str, ranked: list[dedupe.Ranked], issues: dict[int, Issue]
) -> list[dedupe.Ranked]:
    """The vector top 50 of every eval issue in the LLM's order (cached per
    candidate list). Scores stay the vector scores, so they no longer fall."""
    jobs = [
        rerank_mod.Job(
            issues[r.query], [rerank_mod.Seen.of(issues[n]) for n, _ in r.candidates]
        )
        for r in ranked
    ]
    path = get_settings().rerank_dir / f"{repo_slug(repo)}.json"
    records, batch = rerank_mod.rerank_all(
        jobs, path, rerank_mod.OpenAIReranker(openai_key())
    )
    typer.echo(f"  rerank: {batch.sent} reranked, {batch.cached} cached -> {path}")
    rows = list(records.values())
    typer.echo(
        f"  per issue: ${fmean(x.cost_usd for x in rows):.5f} mean, "
        f"{median(x.latency_ms for x in rows) / 1000:.1f} s median latency"
    )
    out: list[dedupe.Ranked] = []
    for r in ranked:
        score = dict(r.candidates)
        out.append(
            replace(r, candidates=[(n, score[n]) for n in records[r.query].order])
        )
    return out


def _verdicts(
    repo: str, ranked: list[dedupe.Ranked], issues: dict[int, Issue]
) -> dict[int, verdict_mod.VerdictRecord]:
    """The checked LLM verdict for every eval issue, cached per candidate list."""
    jobs = [
        verdict_mod.Job(
            query=issues[r.query],
            candidates=[
                verdict_mod.Candidate.of(issues[n], issues[r.query])
                for n, _ in r.candidates
            ],
        )
        for r in ranked
    ]
    path = get_settings().verdicts_dir / f"{repo_slug(repo)}.json"
    judge = verdict_mod.OpenAIJudge(openai_key())
    records, batch = verdict_mod.judge_all(jobs, path, judge)
    typer.echo(f"  verdicts: {batch.sent} judged, {batch.cached} cached -> {path}")
    return records


@eval_app.command("refine")
def eval_refine(
    repo: str = PLAYGROUND,
    only: str | None = None,
    workers: int = 4,
    features: bool = False,
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
    feature_set: Path = Path("playground/features.toml"),
) -> None:
    """Refine on the planted bugs: spec files vs the reference fix's files
    (hit@k, recall@k, precision) and the size guess, per bug size.
    `--features`: on the removed features instead (feature route, criteria;
    results in data/refine/features-*).

    Paid: one agent run per bug, nothing cached; `--only key,key` for a few.
    Needs the seeded issues fetched (`fetch-issues`). The clone is updated to
    the tip of main, the planted code. Every result goes to data/refine/.
    """
    if features:
        _eval_refine_features(repo, only, workers, feature_set)
        return
    settings = get_settings()
    target = check_repo(repo)
    agent = claude_code()
    planted = pick_only(load_bugs(bugs, seed), only, lambda b: b.key, "bug")
    items = [
        s for s in triage_eval.load_seed(seed) if s.key in {b.key for b in planted}
    ]
    matched = triage_eval.match(items, load_issues(settings.issues_dir, target))
    clone = clone_dir(settings.clones_dir, target)
    commit = refine_mod.update_clone(target, clone)
    typer.echo(f"{target} at {commit[:7]}: {len(planted)} planted bugs, {agent.model}")

    def run(key: str) -> refine_mod.Refined | AgentError:
        try:
            return refine_mod.refine(matched[key], clone, agent)
        except AgentError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(
            zip(
                [b.key for b in planted],
                pool.map(run, [b.key for b in planted]),
                strict=True,
            )
        )
    done = {k: r for k, r in results.items() if not isinstance(r, AgentError)}
    scores = [
        refine_eval.score_bug(b, done[b.key].spec) for b in planted if b.key in done
    ]
    for b in planted:
        r = results[b.key]
        if isinstance(r, AgentError):
            typer.echo(f"  {b.key:<30} {b.size}  failed: {r}")
            continue
        sc = next(x for x in scores if x.key == b.key)
        listed = ", ".join(f.path for f in r.spec.files)
        mark = "hit@1" if sc.hit[1] else "hit@3" if sc.hit[3] else "miss "
        typer.echo(
            f"  {b.key:<30} {b.size}->{sc.size_guess}  {mark}  "
            f"${r.run.cost_usd:.3f} {r.run.turns:>2}t  {listed}"
        )
    _refine_summary(scores, [r.run for r in done.values()], len(results) - len(done))
    adapter = TypeAdapter(dict[str, refine_mod.Refined])
    out = write_stamped(settings.refine_dir, "eval", adapter, done)
    typer.echo(f"  -> {out}")


def _refine_summary(
    scores: list[refine_eval.BugScore],
    runs: list[AgentRun],
    failed: int,
    unit: str = "bug",
) -> None:
    for label, group in [
        ("all", scores),
        *[(f"size {z}", [x for x in scores if x.size == z]) for z in ("S", "M", "L")],
    ]:
        t = refine_eval.summarize(group)
        if t.n:
            typer.echo(
                f"  {label:<7} n={t.n:<3} hit@1 {t.hit[1]:.2f}  hit@3 {t.hit[3]:.2f}  "
                f"recall@3 {t.recall[3]:.2f}  precision {t.precision:.2f}  "
                f"size {t.size_ok:.2f}  files listed {t.listed:.1f}"
            )
    if runs:
        typer.echo(
            f"  per {unit}: ${fmean(r.cost_usd for r in runs):.3f} mean, "
            f"{median(r.turns for r in runs):.0f} turns and "
            f"{median(r.latency_ms for r in runs) / 1000:.0f} s median, "
            f"${sum(r.cost_usd for r in runs):.2f} total, "
            f"{failed} failed"
        )


def _eval_refine_features(
    repo: str, only: str | None, workers: int, path: Path
) -> None:
    """`eval refine --features`: refine on each removed feature's state,
    scored like the bugs, plus the criteria next to the removed tests in a
    markdown file for the dev (the executable check is `eval loop
    --features`)."""
    settings = get_settings()
    target = check_repo(repo)
    agent = claude_code()
    feats = pick_only(
        features_eval.load_features(path), only, lambda f: f.key, "feature"
    )
    clone = clone_dir(settings.clones_dir, target)
    commit = refine_mod.update_clone(target, clone)
    typer.echo(f"{target} at {commit[:7]}: {len(feats)} features, {agent.model}")

    def run(f: features_eval.Feature) -> refine_mod.Refined | Exception:
        worktree = settings.work_dir / f"refine-{f.key}"
        try:
            return features_eval.refine_feature(
                f, path, target, clone, commit, worktree, agent
            )
        except (AgentError, refine_mod.NoCriteria) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(zip([f.key for f in feats], pool.map(run, feats), strict=True))
    done = {k: r for k, r in results.items() if isinstance(r, refine_mod.Refined)}
    scores = [
        refine_eval.score_bug(f, done[f.key].spec) for f in feats if f.key in done
    ]
    for f in feats:
        r = results[f.key]
        if not isinstance(r, refine_mod.Refined):
            typer.echo(f"  {f.key:<22} {f.size}  failed: {r}")
            continue
        sc = next(x for x in scores if x.key == f.key)
        listed = ", ".join(x.path for x in r.spec.files)
        mark = "hit@1" if sc.hit[1] else "hit@3" if sc.hit[3] else "miss "
        typer.echo(
            f"  {f.key:<22} {f.size}->{sc.size_guess}  {mark}  "
            f"{len(r.spec.criteria)} criteria  ${r.run.cost_usd:.3f} "
            f"{r.run.turns:>2}t  {listed}"
        )
    runs = [r.run for r in done.values()]
    _refine_summary(scores, runs, len(feats) - len(done), "feature")
    if done:
        typer.echo(
            "  criteria per feature: "
            f"{median(len(r.spec.criteria) for r in done.values()):.0f} median"
        )
    adapter = TypeAdapter(dict[str, refine_mod.Refined])
    out = write_stamped(settings.refine_dir, "features", adapter, done)
    side = out.with_suffix(".md")
    side.write_text(features_eval.side_by_side(path, feats, done), "utf-8")
    typer.echo(f"  -> {out}\n  -> {side}")


@eval_app.command("bug-set")
def eval_bug_set(
    repo: str = PLAYGROUND,
    only: str | None = None,
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """Prove the planted-bug answer key: per bug, its repro red on the tip
    of main (the planted code) and green with only its own reference fix
    (which must apply). Free (no agent), needs podman."""
    settings = get_settings()
    target = check_repo(repo)
    rules = loop_rules(target)
    planted = pick_only(load_bugs(bugs, seed), only, lambda b: b.key, "bug")
    clone = clone_dir(settings.clones_dir, target)
    commit = refine_mod.update_clone(target, clone)
    typer.echo(f"{target} at {commit[:7]}: {len(planted)} planted bugs")
    sandbox = Podman(image=rules.image, setup=rules.setup)
    worktree = settings.work_dir / "bug-set"
    worktree_mod.add_worktree(clone, worktree, commit)
    try:
        if not sandbox(rules.install, worktree, network=True).ok:
            typer.echo("install failed", err=True)
            raise typer.Exit(code=1)
        proofs = prove_bugs(worktree, commit, planted, bugs, rules, sandbox)
    finally:
        worktree_mod.remove_worktree(clone, worktree)
    for b, p in zip(planted, proofs, strict=True):
        mark = "valid  " if p.valid else "INVALID"
        fix = (
            "green with its fix"
            if p.green
            else "fix doesn't apply"
            if not p.applies
            else "RED with its fix"
        )
        planted_state = "red" if p.red else "GREEN"
        typer.echo(f"  {b.key:<30} {b.kind:<5} {mark}  {planted_state} planted, {fix}")
    valid = sum(p.valid for p in proofs)
    typer.echo(f"  {valid}/{len(proofs)} valid")
    if valid < len(proofs):
        raise typer.Exit(code=1)


@eval_app.command("sentinel")
def eval_sentinel(repo: str = PLAYGROUND) -> None:
    """The sentinel's false alarms: every stored right change (a green run
    the hidden judge passed, in eval-* and features-*) checked against the
    target's invariant; a right change it flags is a false alarm. Free; the
    leaks it must catch are unit tests (tests/test_sentinel.py)."""
    settings = get_settings()
    target = check_repo(repo)
    rules = run_mod.load_rules(settings.targets_dir, target)
    found = sentinel_mod.load_sentinel(settings.targets_dir, target)
    if rules is None or found is None:
        typer.echo(f"no [loop] or [sentinel] in the target file of {target}", err=True)
        raise typer.Exit(code=1)
    clone = clone_dir(settings.clones_dir, target)
    check = sentinel_mod.checker(
        found, sentinel_mod.policy_at(clone, "HEAD", found), rules.tests
    )
    checked, alarms = 0, 0
    for path in sorted(settings.runs_dir.glob("*.json")):
        if not path.name.startswith(("eval-", "features-")):
            continue
        stored: dict[str, object] = json_mod.loads(path.read_text("utf-8"))
        for key, value in stored.items():
            try:
                judged = loop_eval.Judged.model_validate(value)
            except ValidationError:
                continue  # an older stored shape
            r = judged.result
            right = r.status == "green" and (judged.judge is None or judged.judge.ok)
            if r.repo != target or not right or not r.diff:
                continue
            checked += 1
            for v in check(r.diff):
                alarms += 1
                typer.echo(f"  false alarm {path.stem}/{key}: {v}")
    typer.echo(f"{target}: {checked} right changes checked, {alarms} false alarms")


@eval_app.command("feature-set")
def eval_feature_set(
    repo: str = PLAYGROUND,
    features: Path = Path("playground/features.toml"),
) -> None:
    """Prove the removed-feature set: per feature, its removal applied to
    the tip of main leaves every check of the target green, and the hidden
    reference tests red; on main itself they are green. Free (no agent),
    needs podman; one install, then each removal in turn."""
    settings = get_settings()
    target = check_repo(repo)
    rules = loop_rules(target)
    feats = features_eval.load_features(features)
    clone = clone_dir(settings.clones_dir, target)
    commit = refine_mod.update_clone(target, clone)
    typer.echo(f"{target} at {commit[:7]}: {len(feats)} removed features")
    sandbox = Podman(image=rules.image, setup=rules.setup)
    worktree = settings.work_dir / "feature-set"
    worktree_mod.add_worktree(clone, worktree, commit)
    try:
        if not sandbox(rules.install, worktree, network=True).ok:
            typer.echo("install failed", err=True)
            raise typer.Exit(code=1)
        tests = sorted({t for f in feats for t in f.tests})
        on_main = sandbox(run_mod.repro_command(rules, tests), worktree, network=False)
        typer.echo(f"  hidden tests on main: {'green' if on_main.ok else 'RED'}")
        proofs = features_eval.prove(worktree, commit, feats, features, rules, sandbox)
    finally:
        worktree_mod.remove_worktree(clone, worktree)
    for f, p in zip(feats, proofs, strict=True):
        failed = [c.name for c in p.checks if not c.ok]
        checks = "checks green" if not failed else f"checks red: {', '.join(failed)}"
        hidden = "hidden red" if not p.hidden.ok else "hidden GREEN"
        mark = "valid  " if p.valid else "INVALID"
        typer.echo(f"  {f.key:<22} {f.size}  {mark}  {checks}, {hidden}")
    valid = sum(p.valid for p in proofs)
    typer.echo(f"  {valid}/{len(proofs)} valid")
    if not on_main.ok or valid < len(proofs):
        raise typer.Exit(code=1)


@eval_app.command("loop")
def eval_loop(
    repo: str = PLAYGROUND,
    only: str | None = None,
    workers: int = 4,
    planted: bool = False,
    wrong: bool = False,
    features: bool = False,
    kind: str = "logic",
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
    feature_set: Path = Path("playground/features.toml"),
) -> None:
    """The core loop on the planted bugs (`--kind logic|ui|all`, logic by
    default; a UI bug needs a Playwright suite in `[\\[loop.suites]]`): the
    agent writes the repro
    (red rate, valid against the reference fix), then fixes; resolved = the
    hidden planted repro and every check green. Also first try, fix in the
    reference's files, cost and time, per bug size. `--planted` hands the
    planted repro to the agent instead (the given-repro baseline).

    Paid: the reproducer's and implementer's runs only. Each bug starts from the spec
    `eval refine` stored in data/refine/ (refine is measured there); a bug
    without one, or with one made at another commit, is reported and
    skipped. Every result goes to data/runs/.

    `--wrong`: the implementer is told to make a plausible fix that does
    not fix the bug (wrong-*.json, the reviewer eval's negatives; a judge
    red is the goal there).

    `--features`: the feature route on the removed features, from the specs
    `eval refine --features` stored: the acceptance test (red before, then
    frozen), built, the hidden reference tests green on the agent's code,
    and the agent's test green on main (results in data/runs/features-*).
    """
    if features:
        _eval_loop_features(repo, only, workers, feature_set)
        return
    if planted and wrong:
        typer.echo("--wrong needs the agent's own repro, not --planted", err=True)
        raise typer.Exit(code=1)
    settings = get_settings()
    target = check_repo(repo)
    rules = loop_rules(target)
    agent = claude_code().editing(budget_usd=1.0)
    if kind not in ("logic", "ui", "all"):
        typer.echo("--kind must be logic, ui or all", err=True)
        raise typer.Exit(code=1)
    logic = [b for b in load_bugs(bugs, seed) if kind in ("all", b.kind)]
    logic = pick_only(logic, only, lambda b: b.key, f"{kind} bug")
    clone = clone_dir(settings.clones_dir, target)
    commit = refine_mod.update_clone(target, clone)
    specs = loop_eval.load_specs(settings.refine_dir)
    ready = [b for b in logic if b.key in specs and specs[b.key].commit == commit]
    for b in logic:
        if b not in ready:
            typer.echo(f"  {b.key:<30} skipped: no stored spec at {commit[:7]}")
    typer.echo(f"{target} at {commit[:7]}: {len(ready)} {kind} bugs, {agent.model}")
    sandbox = Podman(image=rules.image, setup=rules.setup)
    prefix = "wrong" if wrong else "eval"

    def run(bug: PlantedBug) -> loop_eval.Judged:
        refined = specs[bug.key]
        worktree = settings.work_dir / f"{prefix}-{bug.key}"
        guarded = Guarded(agent, Guard(settings.run_budget_usd))  # per bug
        try:
            result = run_mod.run_loop(
                repo=target,
                number=refined.number,
                commit=refined.commit,
                spec=refined.spec,
                clone=clone,
                worktree=worktree,
                repro_file=repro_path(bugs, bug) if planted else None,
                rules=rules,
                agent=guarded,
                sandbox=sandbox,
                instructions=review_eval.WRONG if wrong else run_mod.INSTRUCTIONS,
            )
            return loop_eval.against_key(
                result,
                worktree,
                rules,
                repro_path(bugs, bug),
                fix_path(bugs, bug),
                sandbox,
            )
        finally:
            worktree_mod.remove_worktree(clone, worktree)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(zip([b.key for b in ready], pool.map(run, ready), strict=True))
    scores = [loop_eval.score_bug(b, results[b.key]) for b in ready]
    for sc in scores:
        target_mark = "on target" if sc.on_target else "elsewhere"
        valid = {None: "-", True: "valid", False: "INVALID"}[sc.valid]
        verdict = "resolved" if sc.resolved else sc.status
        if sc.status == "green" and not sc.resolved:
            verdict = "judge red"
        typer.echo(
            f"  {sc.key:<30} {sc.size}  repro {valid:<7} {verdict:<9} "
            f"{sc.attempts} tries  {target_mark:<9}  ${sc.cost_usd:.3f}  "
            f"setup {sc.setup_s:.0f}s agent {sc.agent_s:.0f}s "
            f"checks {sc.checks_s:.0f}s"
        )
    for label, group in [
        ("all", scores),
        *[(f"size {z}", [x for x in scores if x.size == z]) for z in ("S", "M", "L")],
    ]:
        t = loop_eval.summarize(group)
        if t.n:
            typer.echo(
                f"  {label:<7} n={t.n:<3} reproduced {t.reproduced:.2f}  "
                f"valid {t.valid:.2f}  green {t.green:.2f}  "
                f"resolved {t.resolved:.2f}  first try {t.first_try:.2f}  "
                f"on target {t.on_target:.2f}  ${t.cost_usd:.3f}/bug  "
                f"{t.attempts:.0f} tries and {t.seconds:.0f} s median"
            )
    total = sum(sc.cost_usd for sc in scores)
    typer.echo(f"  total ${total:.2f}")
    if wrong:
        kept = sum(review_eval.is_case(j, "wrong") for j in results.values())
        typer.echo(f"  wrong fixes kept (green, judge red): {kept}/{len(results)}")
    adapter = TypeAdapter(dict[str, loop_eval.Judged])
    out = write_stamped(settings.runs_dir, prefix, adapter, results)
    typer.echo(f"  -> {out}")


def _eval_loop_features(repo: str, only: str | None, workers: int, path: Path) -> None:
    settings = get_settings()
    target = check_repo(repo)
    rules = loop_rules(target)
    agent = claude_code().editing(budget_usd=1.0)
    feats = pick_only(
        features_eval.load_features(path), only, lambda f: f.key, "feature"
    )
    clone = clone_dir(settings.clones_dir, target)
    commit = refine_mod.update_clone(target, clone)
    specs = features_eval.load_specs(settings.refine_dir)
    ready = [f for f in feats if f.key in specs and specs[f.key].commit == commit]
    for f in feats:
        if f not in ready:
            typer.echo(f"  {f.key:<22} skipped: no stored spec at {commit[:7]}")
    typer.echo(f"{target} at {commit[:7]}: {len(ready)} features, {agent.model}")
    sandbox = Podman(image=rules.image, setup=rules.setup)

    def run(f: features_eval.Feature) -> loop_eval.Judged:
        refined = specs[f.key]
        worktree = settings.work_dir / f"feature-{f.key}"
        guarded = Guarded(agent, Guard(settings.run_budget_usd))  # per feature
        patch = features_eval.removal_path(path, f.key)
        try:
            result = run_mod.run_loop(
                repo=target,
                number=refined.number,
                commit=refined.commit,
                spec=refined.spec,
                clone=clone,
                worktree=worktree,
                repro_file=None,
                rules=rules,
                agent=guarded,
                sandbox=sandbox,
                route=f.route,
                prepare=partial(features_eval.remove, patch=patch),
            )
            return features_eval.against_reference(result, worktree, f, rules, sandbox)
        finally:
            worktree_mod.remove_worktree(clone, worktree)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(zip([f.key for f in ready], pool.map(run, ready), strict=True))
    scores = [loop_eval.score_bug(f, results[f.key]) for f in ready]
    for sc in scores:
        target_mark = "on target" if sc.on_target else "elsewhere"
        valid = {None: "-", True: "valid", False: "INVALID"}[sc.valid]
        verdict = "hidden green" if sc.resolved else sc.status
        if sc.status == "green" and not sc.resolved:
            verdict = "hidden red"
        typer.echo(
            f"  {sc.key:<22} {sc.size}  test {valid:<7} {verdict:<12} "
            f"{sc.attempts} tries  {target_mark:<9}  ${sc.cost_usd:.3f}  "
            f"setup {sc.setup_s:.0f}s agent {sc.agent_s:.0f}s "
            f"checks {sc.checks_s:.0f}s"
        )
    for label, group in [
        ("all", scores),
        *[(f"size {z}", [x for x in scores if x.size == z]) for z in ("S", "M", "L")],
    ]:
        t = loop_eval.summarize(group)
        if t.n:
            typer.echo(
                f"  {label:<7} n={t.n:<3} test red {t.reproduced:.2f}  "
                f"test valid on main {t.valid:.2f}  built {t.green:.2f}  "
                f"hidden green {t.resolved:.2f}  first try {t.first_try:.2f}  "
                f"on target {t.on_target:.2f}  ${t.cost_usd:.3f}/feature  "
                f"{t.attempts:.0f} tries and {t.seconds:.0f} s median"
            )
    typer.echo(f"  total ${sum(sc.cost_usd for sc in scores):.2f}")
    adapter = TypeAdapter(dict[str, loop_eval.Judged])
    out = write_stamped(settings.runs_dir, "features", adapter, results)
    typer.echo(f"  -> {out}")


@eval_app.command("witness")
def eval_witness(
    repo: str = PLAYGROUND,
    only: str | None = None,
    workers: int = 4,
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """Prove the wrong fixes of `eval loop --wrong` that the planted repro
    missed (judge green): an agent writes a test for what the fix leaves
    broken; confirmed when it is red on the fix and green with the
    reference fix. Paid (one test-writing run per fix, up to 3 tries).
    Results to data/runs/witness-*.json, read by `eval review`."""
    settings = get_settings()
    target = check_repo(repo)
    rules = loop_rules(target)
    agent = claude_code().editing(budget_usd=1.0)
    by_key = {b.key: b for b in load_bugs(bugs, seed)}
    clone = clone_dir(settings.clones_dir, target)
    specs = loop_eval.load_specs(settings.refine_dir)
    wrong = review_eval.load_runs(settings.runs_dir, "wrong")
    todo = [
        k
        for k, j in sorted(wrong.items())
        if review_eval.usable(j) and j.judge is not None and j.judge.ok
    ]
    todo = pick_only(todo, only, lambda k: k, "wrong fix")
    typer.echo(f"{target}: {len(todo)} wrong fixes the planted repro missed")
    sandbox = Podman(image=rules.image, setup=rules.setup)

    def run(key: str) -> witness_eval.Witness:
        worktree = settings.work_dir / f"witness-{key}"
        try:
            return witness_eval.witness(
                judged=wrong[key],
                spec=specs[key].spec,
                rules=rules,
                clone=clone,
                worktree=worktree,
                agent=agent,
                sandbox=sandbox,
                patch=fix_path(bugs, by_key[key]),
            )
        finally:
            worktree_mod.remove_worktree(clone, worktree)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(zip(todo, pool.map(run, todo), strict=True))
    for key, w in results.items():
        if w.confirmed:
            state = "confirmed"
        elif w.error:
            state = w.error
        elif w.red is None:
            state = "no red witness"
        else:
            state = "witness red with the reference fix too"
        typer.echo(f"  {key:<30} {state}  {len(w.attempts)} tries  ${w.cost_usd:.3f}")
    kept = sum(w.confirmed for w in results.values())
    total = sum(w.cost_usd for w in results.values())
    typer.echo(f"  confirmed {kept}/{len(results)}  total ${total:.2f}")
    adapter = TypeAdapter(dict[str, witness_eval.Witness])
    out = write_stamped(settings.runs_dir, "witness", adapter, results)
    typer.echo(f"  -> {out}")


@eval_app.command("human")
def eval_human(
    repo: str = PLAYGROUND,
    publish: bool = False,
    update_bodies: bool = False,
    shuffle: int = 0,
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """Human minutes per merged PR. `--publish` (once, paid nothing, writes
    to GitHub): one draft PR per planted logic bug, 3 of them a proven
    wrong fix, shuffled by `--shuffle`; the set goes to data/human/set.json
    (the answer key: don't open it before reviewing). Without it: the
    timed review, one PR after the other, resumable; run it in your own
    terminal. Nothing is merged; the decision is recorded here."""
    settings = get_settings()
    target = check_repo(repo)
    set_path = settings.human_dir / "set.json"
    records_path = settings.human_dir / "reviews.json"
    if publish:
        _publish_human_set(target, set_path, shuffle, seed, update_bodies)
        return
    if not set_path.is_file():
        typer.echo(f"no {set_path}: run with --publish first", err=True)
        raise typer.Exit(code=1)
    hset = human_eval.HumanSet.model_validate_json(set_path.read_text("utf-8"))
    adapter = TypeAdapter(list[human_eval.Record])
    records = (
        adapter.validate_json(records_path.read_bytes())
        if records_path.is_file()
        else []
    )
    done = {r.url for r in records}
    todo = [e for e in hset.entries if e.url not in done]
    typer.echo(f"{len(done)} of {len(hset.entries)} reviewed")
    choice = click.Choice(["merge", "changes", "close"])
    for i, e in enumerate(todo, len(done) + 1):
        typer.echo(f"\n[{i}/{len(hset.entries)}] {e.url}")
        typer.echo("Review it on GitHub as usual (don't click merge).")
        webbrowser.open(e.url)
        start = time.monotonic()
        typer.prompt("Enter when you have decided", default="", show_default=False)
        seconds = round(time.monotonic() - start, 1)
        decision = typer.prompt("decision", type=choice)
        note = typer.prompt("note (optional)", default="", show_default=False)
        records.append(
            human_eval.Record(url=e.url, seconds=seconds, decision=decision, note=note)
        )
        write_atomic(records_path, adapter.dump_json(records, indent=2))
        typer.echo(f"  {seconds / 60:.1f} min, {decision}")
    t = human_eval.summarize(hset.entries, records)
    typer.echo(
        f"\nn={t.n}  {t.minutes:.1f} min per PR (median)  "
        f"{t.per_merge:.1f} min per merged PR  merged {t.merged}  "
        f"wrong caught {t.caught}/{t.wrong}  right merged {t.right_merged}/"
        f"{t.n - t.wrong}  agreed with the reviewer {t.agreed:.2f}"
    )


def _publish_human_set(
    target: str, set_path: Path, shuffle: int, seed: Path, update: bool
) -> None:
    """Draft PRs for the set; resumable (published picks are skipped, or
    get the current body with `update`)."""
    settings = get_settings()
    token = write_token_for(target)
    rules = loop_rules(target)
    found = review_eval.cases(
        settings.runs_dir, witness_eval.load_witnessed(settings.runs_dir)
    )
    runs = {(k, kind): j for k, kind, j in found}
    reviews = review_eval.load_reviews(settings.runs_dir)
    if set_path.is_file():
        hset = human_eval.HumanSet.model_validate_json(set_path.read_text("utf-8"))
    else:
        right = [k for k, kind, _ in found if kind == "right"]
        wrong = [k for k, kind, _ in found if kind == "wrong"]
        hset = human_eval.HumanSet(picks=human_eval.pick(right, wrong, shuffle))
    missing = [p for p in hset.picks if (p.key, p.kind) not in reviews]
    if missing:
        names = ", ".join(f"{p.kind} {p.key}" for p in missing)
        typer.echo(f"no stored review for {names}: run eval review", err=True)
        raise typer.Exit(code=1)
    specs = loop_eval.load_specs(settings.refine_dir)
    titles = {s.key: s.title for s in triage_eval.load_seed(seed)}
    clone = clone_dir(settings.clones_dir, target)
    pulls = publish_mod.GitHubPulls(token)
    published = {(e.key, e.kind): e for e in hset.entries}
    set_path.parent.mkdir(parents=True, exist_ok=True)
    for p in hset.picks:
        if (p.key, p.kind) in published and not update:
            continue
        result = runs[(p.key, p.kind)].result
        review = reviews[(p.key, p.kind)]
        draft = record_mod.draft_for(
            settings.targets_dir,
            target,
            title=titles[p.key],
            spec=specs[p.key].spec,
            result=result,
            tests=rules.tests,
            review=review,
        )
        entry = published.get((p.key, p.kind))
        if entry is not None:
            pulls.update_body(target, entry.pr, draft.body)
            typer.echo(f"  #{result.number}: body updated on {entry.url}")
            continue
        try:
            got = publish_mod.publish(
                repo=target,
                clone=clone,
                base_commit=result.commit,
                diff=result.diff,
                number=result.number,
                issue_title=titles[p.key],
                title=draft.title,
                body=draft.body,
                token=token,
                pulls=pulls,
            )
        except (publish_mod.PublishError, httpx.HTTPError) as exc:
            set_path.write_text(hset.model_dump_json(indent=2), "utf-8")
            typer.echo(f"  #{result.number}: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        hset.entries.append(
            human_eval.Entry(
                key=p.key,
                kind=p.kind,
                number=result.number,
                pr=got.number,
                url=got.url,
                branch=got.branch,
                approved=review.approved,
            )
        )
        set_path.write_text(hset.model_dump_json(indent=2), "utf-8")
        typer.echo(f"  #{result.number} -> {got.url}")
    typer.echo(f"{len(hset.entries)} draft PRs, set in {set_path}")


@eval_app.command("gate")
def eval_gate(
    repo: str = PLAYGROUND,
    only: str | None = None,
    workers: int = 4,
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """The review gate on fixes with a known verdict: right ones should be
    published, wrong ones blocked or fixed, never leaked (published and
    still wrong by the planted repro or the witness test). Proofs checked
    against the reference fix. Each case replays its diff with an install;
    paid (reviews, proofs and fix rounds; about $0.15-0.40 per case)."""
    settings = get_settings()
    target = check_repo(repo)
    rules = loop_rules(target)
    reader = claude_code()
    editor = reader.editing(budget_usd=1.0)
    by_key = {b.key: b for b in load_bugs(bugs, seed)}
    clone = clone_dir(settings.clones_dir, target)
    specs = loop_eval.load_specs(settings.refine_dir)
    witnesses = witness_eval.load_witnesses(settings.runs_dir)
    witnessed = frozenset(k for k, w in witnesses.items() if w.confirmed)
    todo = review_eval.cases(settings.runs_dir, witnessed)
    todo = pick_only(todo, only, lambda c: c[0], "case")
    typer.echo(
        f"{target}: {sum(k == 'right' for _, k, _ in todo)} right, "
        f"{sum(k == 'wrong' for _, k, _ in todo)} wrong fixes, {reader.model}"
    )
    sandbox = Podman(image=rules.image, setup=rules.setup)

    def run(case: tuple[str, review_eval.Kind, loop_eval.Judged]) -> gate_eval.GateCase:
        key, kind, judged = case
        worktree = settings.work_dir / f"gate-{kind}-{key}"
        try:
            return gate_eval.gate_case(
                key=key,
                kind=kind,
                judged=judged,
                spec=specs[key].spec,
                rules=rules,
                clone=clone,
                worktree=worktree,
                reader=reader,
                editor=editor,
                sandbox=sandbox,
                planted=repro_path(bugs, by_key[key]),
                patch=fix_path(bugs, by_key[key]),
                witness=witnesses.get(key),
            )
        finally:
            worktree_mod.remove_worktree(clone, worktree)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, todo))
    for c in results:
        g = c.gated
        if g is None:
            typer.echo(f"  {c.kind:<5} {c.key:<30} setup failed: {c.error}")
            continue
        pv = c.proofs_valid
        valid = "-" if pv is None else ("valid" if pv.ok else "INVALID")
        typer.echo(
            f"  {c.kind:<5} {c.key:<30} {g.status:<9} {c.outcome:<11} "
            f"{len(g.rounds)} reviews {c.proven} proven (proofs {valid})  "
            f"${g.cost_usd:.3f}"
        )
    t = gate_eval.summarize(results)
    typer.echo(
        f"  right n={t.right} published {t.published:.2f}  wrong n={t.wrong} "
        f"blocked {t.blocked:.2f} fixed {t.fixed:.2f} leaked {t.leaked:.2f}  "
        f"proofs {t.proofs} valid {t.proofs_valid:.2f}  ${t.cost_usd:.3f}/case"
    )
    adapter = TypeAdapter(list[gate_eval.GateCase])
    out = write_stamped(settings.runs_dir, "gate", adapter, results)
    typer.echo(f"  -> {out}")


@eval_app.command("review")
def eval_review(
    repo: str = PLAYGROUND,
    only: str | None = None,
    workers: int = 4,
) -> None:
    """The reviewer on fixes with a known verdict: right ones (`eval loop`,
    judge green) should be approved, wrong ones (`eval loop --wrong`, judge
    red) not. Each stored diff is replayed in a fresh worktree, so this
    pays for reviewer runs only (about $0.04 each). Results to data/runs/."""
    settings = get_settings()
    target = check_repo(repo)
    rules = loop_rules(target)
    agent = claude_code()
    clone = clone_dir(settings.clones_dir, target)
    specs = loop_eval.load_specs(settings.refine_dir)
    todo = review_eval.cases(
        settings.runs_dir, witness_eval.load_witnessed(settings.runs_dir)
    )
    todo = pick_only(todo, only, lambda c: c[0], "case")
    typer.echo(
        f"{target}: {sum(k == 'right' for _, k, _ in todo)} right, "
        f"{sum(k == 'wrong' for _, k, _ in todo)} wrong fixes, {agent.model}"
    )

    def run(case: tuple[str, review_eval.Kind, loop_eval.Judged]) -> review_eval.Case:
        key, kind, judged = case
        return review_eval.review_case(
            key=key,
            kind=kind,
            judged=judged,
            spec=specs[key].spec,
            rules=rules,
            clone=clone,
            worktree=settings.work_dir / f"review-{kind}-{key}",
            agent=agent,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, todo))
    for c in results:
        if c.review is None:
            typer.echo(f"  {c.kind:<5} {c.key:<30} failed: {c.error}")
            continue
        r = c.review
        good = c.approved == (c.kind == "right")
        verdict = "approved" if c.approved else "rejected"
        typer.echo(
            f"  {c.kind:<5} {c.key:<30} {verdict:<9} {'ok ' if good else 'MISS'} "
            f"test {'y' if r.reviewed.test_matches_spec else 'n'} "
            f"cause {'y' if r.reviewed.fixes_cause else 'n'} "
            f"{len(r.reviewed.findings)} findings  ${r.run.cost_usd:.3f}"
        )
    t = review_eval.summarize(results)
    typer.echo(
        f"  right n={t.right} passed {t.passed:.2f}  wrong n={t.wrong} caught "
        f"{t.caught:.2f}  failed runs {t.failed}  ${t.cost_usd:.3f}/review"
    )
    adapter = TypeAdapter(list[review_eval.Case])
    out = write_stamped(settings.runs_dir, "review", adapter, results)
    typer.echo(f"  -> {out}")
