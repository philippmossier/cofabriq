"""The core loop's commands: refine, run, the flow and its gates, intake,
lessons, the kill switch, and the API the viewer and the browser gate use."""

import contextlib
import json as json_mod
import typing
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
import uvicorn
from fastapi import FastAPI
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from rich.console import Console

from cofabriq.cli.common import (
    app,
    claude_code,
    flow_app,
    github,
    loop_rules,
    target_repo,
    write_token_for,
)
from cofabriq.cli.render import show_draft, show_gate, show_loop, show_spec
from cofabriq.config import get_settings
from cofabriq.eval import loop as loop_eval
from cofabriq.eval.bugs import fix_path, load_bugs, planted_for, repro_path
from cofabriq.files import stamp
from cofabriq.git import files_at
from cofabriq.issues import triage as triage_mod
from cofabriq.issues.fetch import load_issue
from cofabriq.issues.model import Issue, IssueRef, check_repo
from cofabriq.loop import gate as gate_mod
from cofabriq.loop import graph as graph_mod
from cofabriq.loop import intake as intake_mod
from cofabriq.loop import lessons as lessons_mod
from cofabriq.loop import pr as pr_mod
from cofabriq.loop import publish as publish_mod
from cofabriq.loop import record as record_mod
from cofabriq.loop import refine as refine_mod
from cofabriq.loop import run as run_mod
from cofabriq.loop import worktree as worktree_mod
from cofabriq.loop.engine import (
    STOP_ENV,
    AgentError,
    Guard,
    Guarded,
    Halted,
    stop_reason,
)
from cofabriq.loop.sandbox import Podman
from cofabriq.serve import build as build_api
from cofabriq.serve_guard import new_token
from cofabriq.steps import Steps
from cofabriq.targets import PLAYGROUND, clone_dir, repo_slug


@app.command()
def refine(number: int, repo: str | None = None, json: bool = False) -> None:
    """Spec for one issue from a read-only agent reading the code (paid, one
    agent run). Updates the clone in data/clones/ first and fetches the issue
    if it is new or changed on GitHub (needs GITHUB_TOKEN)."""
    settings = get_settings()
    target = target_repo(repo)
    agent = claude_code()
    steps = Steps(Console(stderr=True, soft_wrap=True))
    clone = clone_dir(settings.clones_dir, target)
    with steps.step("clone", f"updating {clone}") as done:
        commit = refine_mod.update_clone(target, clone)
        done(f"{clone} at {commit[:7]}")
    issue = _refresh_issue(target, number, steps)
    if issue is None:
        typer.echo(f"#{number} is not in {settings.issues_dir}", err=True)
        raise typer.Exit(code=1)
    with steps.step("agent", f"reading the code ({agent.model})") as done:
        try:
            refined = refine_mod.refine(issue, clone, agent)
        except AgentError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        r = refined.run
        done(f"{r.turns} turns, {r.latency_ms / 1000:.0f} s, ${r.cost_usd:.3f}")
    if refined.unknown_paths:
        steps.note(
            "paths",
            f"not in {commit[:7]}: {', '.join(refined.unknown_paths)}",
            ok=False,
        )
    else:
        steps.note("paths", "all in the commit")
    if json:
        typer.echo(refined.model_dump_json(indent=2))
    else:
        show_spec(refined)


def _refresh_issue(repo: str, number: int, steps: Steps) -> Issue | None:
    """The stored issue, refreshed from GitHub first if there is a token."""
    settings = get_settings()
    source = github(repo)
    if source is None:
        steps.note("GitHub", "skipped: no GITHUB_TOKEN, local copy", ok=False)
        return load_issue(settings.issues_dir, repo, number)
    with steps.step("GitHub", f"checking #{number}") as done:
        got = triage_mod.refresh_issue(source, settings.issues_dir, number)
        done(triage_mod.fetched(number, got))
    return got[0] if got is not None else load_issue(settings.issues_dir, repo, number)


@contextlib.contextmanager
def _plain_step(steps: Steps, name: str) -> Generator[Callable[[str], None]]:
    """A step without a spinner, for one that asks the dev something."""
    yield lambda detail: steps.note(name, detail)


def _approve_test(test_diff: str) -> bool:
    """The test gate: the frozen acceptance test, before any code."""
    typer.echo("acceptance test (frozen once approved):")
    typer.echo(test_diff)
    return typer.confirm("Approve the test and let the agent build against it?")


def _blocker_issues(tickets: list[Issue]) -> dict[IssueRef, Issue]:
    """The tickets' blockers as GitHub has them now (read-only; the local
    store without a token). A blocker that can't be read stays out: it then
    counts as open."""
    known: dict[IssueRef, Issue] = {}
    for ref in dict.fromkeys(b for t in tickets for b in intake_mod.blockers(t)):
        source = github(ref.repo)
        if source is None:
            got = load_issue(get_settings().issues_dir, ref.repo, ref.number)
        else:
            got = source.issue(ref.number)
        if got is not None:
            known[ref] = got
    return known


def _open_prs(repo: str) -> list[int] | None:
    """cofabriq's open PRs in `repo`; None without a token (unknown)."""
    source = github(repo)
    return None if source is None else source.open_pulls(publish_mod.BRANCH_PREFIX)


def _api(
    ui: Path | None,
    port: int | None = None,
    write_token: str | None = None,
    flows: graph_mod.FlowRunner | None = None,
) -> FastAPI:
    settings = get_settings()
    return build_api(
        settings.refine_dir,
        settings.runs_dir,
        settings.clones_dir,
        ui,
        port=port,
        write_token=write_token,
        flows=flows,
    )


def _no_deps(repo: str, spent: float) -> graph_mod.Deps:
    raise RuntimeError("the schema export runs no flow")


@app.command()
def serve(port: int = 8765, ui: Path = Path("ui/dist"), write: bool = False) -> None:
    """The API over data/refine and data/runs, plus the built UI
    (`ui/dist`, when built), on 127.0.0.1 only. API docs at /api/docs.
    Read-only, unless `--write`: then the browser gate answers flows' gates
    (the token is printed once)."""

    typer.echo(f"http://127.0.0.1:{port}  (API docs: /api/docs; Ctrl+C stops)")
    if not write:  # no token: the guard refuses every non-GET
        uvicorn.run(_api(ui, port), host="127.0.0.1", port=port, log_level="warning")
        return
    # --write: the browser gate. The token is shown once, in the URL's
    # fragment (never sent to the server, so in no log)
    token = new_token()
    runner = graph_mod.FlowRunner(
        get_settings().postgres_dsn, lambda repo, spent: _flow_deps(repo, spent)[0]
    )
    typer.echo(f"gate: http://127.0.0.1:{port}/#token={token}  (open this one)")
    app = _api(ui, port, write_token=token, flows=runner)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


@app.command()
def openapi(out: Path = Path("ui/openapi.json")) -> None:
    """Write the API's OpenAPI schema (the UI's generated client reads it).
    `make check` fails when the committed one is stale."""
    out.parent.mkdir(parents=True, exist_ok=True)
    flows = graph_mod.FlowRunner("", _no_deps)  # for its routes in the schema
    schema = _api(None, flows=flows).openapi()
    out.write_text(json_mod.dumps(schema, indent=2) + "\n", "utf-8")
    typer.echo(f"-> {out}")


def _flow_deps(target: str, spent_usd: float = 0.0) -> tuple[graph_mod.Deps, str]:
    settings = get_settings()
    rules = loop_rules(target)
    clone = clone_dir(settings.clones_dir, target)
    guard = Guard(settings.run_budget_usd, spent_usd)
    reader = Guarded(claude_code(), guard)
    deps = graph_mod.Deps(
        clone=clone,
        work_dir=settings.work_dir,
        rules=rules,
        reader=reader,
        editor=reader.editing(budget_usd=1.0),
        sandbox=Podman(image=rules.image, setup=rules.setup),
        guard=guard,
        targets_dir=settings.targets_dir,
        runs_dir=settings.runs_dir,
        lessons_dir=settings.lessons_dir,
    )
    return deps, settings.postgres_dsn


def _flow_report(graph: "CompiledStateGraph[graph_mod.FlowState]", thread: str) -> None:
    """Where the run stands: the gate it waits at, or how it ended."""
    found = graph_mod.read_status(graph, thread)
    if found is None:
        typer.echo(f"no flow for {thread}")
        return
    state = found.state
    if found.waiting:
        typer.echo(f"{thread}: waiting at the {found.waiting} gate")
        if found.waiting == "plan" and state.refined is not None:
            show_spec(state.refined)
        if found.waiting == "test":
            typer.echo(found.diff)
        typer.echo(f"-> cofabriq flow resume {state.issue.number} --approve | --reject")
        return
    note = f" ({state.note})" if state.note else ""
    typer.echo(f"{thread}: done, {state.outcome}{note}")


@flow_app.command("start")
def flow_start(
    number: int,
    repo: str = PLAYGROUND,
    issue_type: str = typer.Option("bug", "--type"),
    restart: bool = False,
) -> None:
    """Refine, then pause at the plan gate (the process exits)."""
    route = _route(issue_type)
    target = check_repo(repo)
    steps = Steps(Console(stderr=True, soft_wrap=True))
    issue = _refresh_issue(target, number, steps)
    if issue is None:
        typer.echo(f"#{number} is not in the issue store", err=True)
        raise typer.Exit(code=1)
    refine_mod.update_clone(target, clone_dir(get_settings().clones_dir, target))
    deps, dsn = _flow_deps(target)
    thread = graph_mod.thread_id(issue)
    with graph_mod.compiled(deps, dsn) as graph:
        if graph.get_state({"configurable": {"thread_id": thread}}).values:
            if not restart:
                typer.echo(f"{thread} has a flow: `flow show`, or --restart", err=True)
                raise typer.Exit(code=1)
            graph.checkpointer.delete_thread(thread)  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]
        state = graph_mod.FlowState(issue=issue, route=route)
        graph.invoke(state, {"configurable": {"thread_id": thread}})
        _flow_report(graph, thread)


@flow_app.command("resume")
def flow_resume(
    number: int,
    repo: str = PLAYGROUND,
    approve: bool = False,
    reject: bool = False,
    reason: str = "",
) -> None:
    """Answer the gate the flow waits at; it runs to the next gate or the end.
    `--reason` (with --reject) is kept for `cofabriq lessons`."""
    if approve == reject:
        typer.echo("--approve or --reject", err=True)
        raise typer.Exit(code=1)
    target = check_repo(repo)
    thread = f"{target}#{number}"
    config: RunnableConfig = {"configurable": {"thread_id": thread}}
    deps, dsn = _flow_deps(target)
    with graph_mod.compiled(deps, dsn) as graph:  # read what was spent so far
        found = graph_mod.read_status(graph, thread)
    spent = found.state.spent_usd if found else 0.0
    deps, dsn = _flow_deps(target, spent)
    with graph_mod.compiled(deps, dsn) as graph:
        if not graph.get_state(config).next:
            typer.echo(f"{thread} waits at no gate", err=True)
            raise typer.Exit(code=1)
        answer = graph_mod.GateAnswer(approve=approve, reason=reason)
        graph.invoke(Command(resume=answer.model_dump()), config)
        _flow_report(graph, thread)


@flow_app.command("show")
def flow_show(number: int, repo: str = PLAYGROUND) -> None:
    """Where a flow stands, read from its checkpoints."""
    target = check_repo(repo)
    deps, dsn = _flow_deps(target)
    with graph_mod.compiled(deps, dsn) as graph:
        _flow_report(graph, f"{target}#{number}")


@app.command()
def lessons(repo: str = PLAYGROUND, propose: bool = False) -> None:
    """What went wrong (findings the review gate proved, plans and tests the
    dev rejected) and, with --propose, rules for the target's CLAUDE.md that
    would have prevented it, each citing its signals: a draft in
    data/lessons/ for the dev to read, never written into the target (paid:
    one read-only agent run)."""
    settings = get_settings()
    target = check_repo(repo)
    signals = lessons_mod.collect(settings.runs_dir, settings.lessons_dir, target)
    for s in signals:
        typer.echo(f"  {s.id:<4} {s.kind:<15} {s.where[:40]:<40} {s.text[:90]}")
    typer.echo(f"{len(signals)} signals for {target}")
    if not propose or not signals:
        return
    clone = clone_dir(settings.clones_dir, target)
    refine_mod.update_clone(target, clone)
    draft = lessons_mod.propose(
        target, signals, clone, Guarded(claude_code(), Guard(1.0))
    )
    out = settings.lessons_dir / f"{repo_slug(target)}-{stamp()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(lessons_mod.render(draft), "utf-8")
    for n, r in enumerate(draft.rules, 1):
        typer.echo(f"  {n}. {r.rule}  ({', '.join(r.signals)})")
    if draft.dropped:
        typer.echo(f"  dropped {len(draft.dropped)} rule(s) citing no known signal")
    typer.echo(f"-> {out}  (${draft.run.cost_usd:.3f})")


@app.command()
def stop(clear: bool = False) -> None:
    """The kill switch: no agent starts while the stop file exists (every
    run and eval in progress ends at its next agent call, status halted).
    `--clear` removes it. `COFABRIQ_STOP=1` does the same per shell."""
    path = get_settings().stop_file
    if clear:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"stopped at {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC\n")
    reason = stop_reason(path)
    typer.echo(
        f"agents stopped: {reason}" if reason else f"agents may run ({STOP_ENV} unset)"
    )


@app.command()
def intake(numbers: list[int], repo: str = PLAYGROUND) -> None:
    """Tickets the dev filed from their own planning session, in the order
    given: done, blocked (an open blocker: GitHub's dependencies or a
    `Blocked by #N` line), waiting (a cofabriq PR is still open: one at a
    time) or ready for `run`. Read-only."""
    target = check_repo(repo)
    steps = Steps(Console(stderr=True, soft_wrap=True))
    tickets: list[Issue] = []
    for n in numbers:
        issue = _refresh_issue(target, n, steps)
        if issue is None:
            typer.echo(f"#{n} is not an issue of {target}", err=True)
            raise typer.Exit(code=1)
        tickets.append(issue)
    prs = _open_prs(target)
    if prs is None:
        steps.note("PRs", "skipped: no GITHUB_TOKEN, open PRs unknown", ok=False)
    for sl in intake_mod.slices(tickets, _blocker_issues(tickets), prs or []):
        why = f"  {sl.reason}" if sl.reason else ""
        typer.echo(f"  #{sl.number:<5} {sl.state:<8}{why}  {sl.title}")


def _route(issue_type: str) -> refine_mod.Route:
    types = typing.get_args(triage_mod.IssueType.__value__)
    if issue_type not in types:
        typer.echo(f"--type must be one of {', '.join(types)}", err=True)
        raise typer.Exit(code=1)
    route = refine_mod.route_for(issue_type)
    if route is None:
        typer.echo(f"a {issue_type} is not for the loop: a person answers it", err=True)
        raise typer.Exit(code=1)
    return route


@app.command()
def run(
    number: int,
    repo: str = PLAYGROUND,
    spec: Path | None = None,
    yes: bool = False,
    keep: bool = False,
    planted: bool = False,
    apply: bool = False,
    issue_type: str = typer.Option("bug", "--type"),
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """The core loop for one issue: refine, plan gate, the agent writes a
    failing test, then fixes the code, repro and checks in a container, max
    3 attempts each (paid). On a planted logic bug the planted repro judges
    the result afterwards; `--planted` hands it to the agent instead.
    `--spec` takes a `refine --json` file instead of a new refine run (the
    dev may edit it); `--yes` skips the plan gate; `--keep` leaves the
    worktree in data/work/. When green, a reviewer reads the change and the
    PR body goes to data/runs/ with the result; `--apply` also pushes a new
    branch and opens a draft PR (playground only, GITHUB_WRITE_TOKEN).
    `--type` is the triage card's type: bug or feature (a feature spec
    comes with acceptance criteria); anything else is not for the loop."""
    route = _route(issue_type)
    settings = get_settings()
    target = check_repo(repo)
    rules = loop_rules(target)
    write_token = write_token_for(target) if apply else None  # before paying
    # every agent of this run (refine, test, build, review, proofs) shares
    # one budget: the kill switch's run cap
    agent = Guarded(claude_code(), Guard(settings.run_budget_usd))
    steps = Steps(Console(stderr=True, soft_wrap=True))
    clone = clone_dir(settings.clones_dir, target)
    with steps.step("clone", f"updating {clone}") as done:
        commit = refine_mod.update_clone(target, clone)
        done(f"{clone} at {commit[:7]}")
    issue = _refresh_issue(target, number, steps)
    if issue is None:
        typer.echo(f"#{number} is not in {settings.issues_dir}", err=True)
        raise typer.Exit(code=1)
    (ticket,) = intake_mod.slices([issue], _blocker_issues([issue]), [])
    if ticket.state == "blocked":  # before paying for refine
        typer.echo(f"#{number} {ticket.reason}: start it once that is merged", err=True)
        raise typer.Exit(code=1)
    bug = planted_for(issue.title, load_bugs(bugs, seed), seed)
    if bug is not None and bug.kind == "ui" and rules.suite(bug.repro) is None:
        bug = None  # the target has no suite to run a Playwright repro
    if planted and bug is None:
        typer.echo(f"#{number} is not a planted bug the loop can run", err=True)
        raise typer.Exit(code=1)
    if bug is not None:
        role = "handed to the agent" if planted else "judges the result"
        steps.note("repro", f"planted {bug.key} ({bug.repro}) {role}")
    if spec is not None:
        refined = refine_mod.Refined.model_validate_json(spec.read_text("utf-8"))
        if (refined.repo, refined.number) != (target, number):
            typer.echo(f"{spec} is for {refined.repo}#{refined.number}", err=True)
            raise typer.Exit(code=1)
        steps.note("spec", f"from {spec} (at {refined.commit[:7]})")
    else:
        with steps.step("refine", f"reading the code ({agent.model})") as done:
            try:
                refined = refine_mod.refine(issue, clone, agent, route)
            except (AgentError, Halted, refine_mod.NoCriteria) as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=1) from exc
            r = refined.run
            done(f"{r.turns} turns, {r.latency_ms / 1000:.0f} s, ${r.cost_usd:.3f}")
    show_spec(refined)
    reasons = intake_mod.not_ready(refined, set(files_at(clone, refined.commit)))
    if reasons:  # back to the dev: edit the spec (--spec) or the ticket
        typer.echo(f"not ready for the loop: {'; '.join(reasons)}", err=True)
        raise typer.Exit(code=1)
    asked = len(refined.spec.open_questions)
    if yes and asked:  # nobody at the gate to answer them
        typer.echo(f"{asked} open question(s): answer them at the gate", err=True)
        raise typer.Exit(code=1)
    question = "Approve the plan and let the agent edit?"
    if asked:
        question = f"Approve the plan as the answer to its {asked} open question(s)?"
    if not yes and not typer.confirm(question):
        raise typer.Exit(code=1)
    worktree = settings.work_dir / f"{repo_slug(target)}-{number}"
    sandbox = Podman(image=rules.image, setup=rules.setup)
    stem = f"{repo_slug(target)}-{number}-{stamp()}"
    test_gate = refined.route == "feature" and not yes
    loop_step = (
        _plain_step(steps, "loop")  # the test gate prompts: no spinner over it
        if test_gate
        else steps.step("loop", f"repro, fix, checks in {worktree}")
    )
    with loop_step as done:
        try:
            result = run_mod.run_loop(
                repo=target,
                number=number,
                commit=refined.commit,
                spec=refined.spec,
                clone=clone,
                worktree=worktree,
                repro_file=repro_path(bugs, bug) if planted and bug else None,
                rules=rules,
                agent=agent.editing(budget_usd=1.0),
                sandbox=sandbox,
                route=refined.route,
                approve_test=_approve_test if test_gate else None,
            )
            # the review gate on the green tree, before the judge resets it:
            # review, findings proven by a test, fixed, reviewed again
            gated = None
            if result.status == "green":
                try:
                    gated = gate_mod.review_gate(
                        spec=refined.spec,
                        rules=rules,
                        result=result,
                        worktree=worktree,
                        reader=agent,
                        editor=agent.editing(budget_usd=1.0),
                        sandbox=sandbox,
                        sentinel=record_mod.sentinel_for(
                            settings.targets_dir,
                            target,
                            clone,
                            refined.commit,
                            rules.tests,
                        ),
                    )
                except Halted as exc:  # no review, so no PR body either
                    typer.echo(f"halted before the review was done: {exc}", err=True)
            # screenshots of the final change, before the judge resets the code
            evidence: list[str] = []
            if result.status == "green" and not (gated and gated.status == "blocked"):
                final = gated.result if gated else result
                evidence = run_mod.collect_evidence(
                    rules, worktree, final.repro, sandbox, settings.runs_dir / stem
                )
            # no verdicts by default: an issue without a planted bug has no
            # answer key, and with --planted the agent already had the repro
            judged = loop_eval.Judged(result=result)
            if bug is not None and not planted:
                if keep:  # validity resets the code: keep the fix to look at
                    judged.judge = loop_eval.judge(
                        worktree, rules, repro_path(bugs, bug), sandbox
                    )
                else:
                    judged = loop_eval.against_key(
                        result,
                        worktree,
                        rules,
                        repro_path(bugs, bug),
                        fix_path(bugs, bug),
                        sandbox,
                    )
        finally:
            if not keep:
                worktree_mod.remove_worktree(clone, worktree)
        tries = len(result.attempts)
        done(f"{result.status}, {tries} attempts, ${result.cost_usd:.3f}")
    show_loop(judged)
    record = record_mod.RunRecord(
        result=judged.result, judge=judged.judge, valid=judged.valid, evidence=evidence
    )
    if gated is not None:
        record.gate = gated
        show_gate(gated)
    if gated is not None and gated.status != "blocked":
        draft = record_mod.draft_for(
            settings.targets_dir,
            target,
            title=issue.title,
            spec=refined.spec,
            result=gated.result,
            tests=rules.tests,
            review=gated.review,
            rounds=gated.rounds,
            route=refined.route,
            evidence=len(evidence),
        )
        record.draft = draft
        body = settings.runs_dir / f"{stem}.pr.md"  # written with the record
        show_draft(draft)
        if write_token is None:
            typer.echo(f"-> {body} (dry run: nothing sent to GitHub)")
        else:
            typer.echo(f"-> {body}")
            record.published = _publish(  # a PR or the error: the run is kept
                target, clone, issue, gated.result, draft, write_token, steps
            )
    out = record_mod.write_record(settings.runs_dir, stem, record)
    typer.echo(f"-> {out}")
    blocked = gated is not None and gated.status == "blocked"
    judged_red = judged.judge is not None and not judged.judge.ok
    if result.status != "green" or blocked or judged_red:
        raise typer.Exit(code=2)


def _publish(
    repo: str,
    clone: Path,
    issue: Issue,
    result: run_mod.LoopResult,
    draft: pr_mod.Draft,
    token: str,
    steps: Steps,
) -> dict[str, object]:
    """The PR as a dict, or `{"error": ...}` (the run is saved either way)."""
    try:
        with steps.step("publish", f"branch + draft PR on {repo}") as done:
            published = publish_mod.publish(
                repo=repo,
                clone=clone,
                base_commit=result.commit,
                diff=result.diff,
                number=result.number,
                issue_title=issue.title,
                title=draft.title,
                body=draft.body,
                token=token,
                pulls=publish_mod.GitHubPulls(token),
            )
            done(f"{published.url} ({published.branch})")
    except (publish_mod.PublishError, httpx.HTTPError) as exc:
        typer.echo(str(exc), err=True)
        return {"error": str(exc)}
    return published.model_dump(mode="json")
