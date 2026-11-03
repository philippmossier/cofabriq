"""The core loop's commands: refine, run, the flow and its gates, intake,
lessons, the kill switch, and the API the viewer and the browser gate use."""

import json as json_mod
import typing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
import uvicorn
from fastapi import FastAPI
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
from cofabriq.issues import triage as triage_mod
from cofabriq.issues.fetch import load_issue
from cofabriq.issues.model import Issue, IssueRef, check_repo
from cofabriq.loop import graph as graph_mod
from cofabriq.loop import intake as intake_mod
from cofabriq.loop import lessons as lessons_mod
from cofabriq.loop import pr as pr_mod
from cofabriq.loop import publish as publish_mod
from cofabriq.loop import record as record_mod
from cofabriq.loop import refine as refine_mod
from cofabriq.loop import run as run_mod
from cofabriq.loop.engine import (
    STOP_ENV,
    AgentError,
    Guard,
    Guarded,
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


def _gate_answer(question: str) -> graph_mod.GateAnswer:
    """The dev's answer at a gate in the terminal: a no asks why (Enter
    skips), the same reason the flow's gates keep for `lessons`."""
    if typer.confirm(question):
        return graph_mod.GateAnswer(approve=True)
    reason: str = typer.prompt(
        "Why? (optional, kept for cofabriq lessons)", default="", show_default=False
    )
    return graph_mod.GateAnswer(approve=False, reason=reason)


def _terminal_gate(at: graph_mod.FlowStatus, yes: bool) -> graph_mod.GateAnswer:
    """`run`'s answer at the gate its graph waits at: `--yes` approves
    (but leaves no open question unanswered), else the dev in the terminal.
    The test gate shows the frozen acceptance test, before any code."""
    assert at.state.refined is not None
    asked = len(at.state.refined.spec.open_questions)
    if at.waiting == "plan" and yes and asked:  # nobody at the gate to answer
        typer.echo(f"{asked} open question(s): answer them at the gate", err=True)
        raise typer.Exit(code=1)
    if yes:
        return graph_mod.GateAnswer(approve=True)
    if at.waiting == "test":
        typer.echo("acceptance test (frozen once approved):")
        typer.echo(at.diff)
        return _gate_answer("Approve the test and let the agent build against it?")
    if asked:
        return _gate_answer(
            f"Approve the plan as the answer to its {asked} open question(s)?"
        )
    return _gate_answer("Approve the plan and let the agent edit?")


def _invoke(
    graph: "CompiledStateGraph[graph_mod.FlowState]",
    given: graph_mod.FlowState | graph_mod.GateAnswer,
    thread: str,
) -> graph_mod.FlowStatus:
    """Start the graph, or resume it with the dev's answer, to its next
    gate or its end; where it stands then."""
    config = graph_mod.thread_config(thread)
    if isinstance(given, graph_mod.GateAnswer):
        graph.invoke(Command(resume=given.model_dump()), config)
    else:
        graph.invoke(given, config)
    now = graph_mod.read_status(graph, thread)
    assert now is not None
    return now


def _answer_key(
    repro: Path, fix: Path, keep: bool, deps: graph_mod.Deps
) -> graph_mod.Judge:
    """`run`'s judge on a planted bug: the hidden planted repro on the tree
    the loop left, and the agent's repro with the reference fix."""
    rules, sandbox = deps.rules, deps.sandbox

    def judge(
        result: run_mod.LoopResult, worktree: Path
    ) -> tuple[run_mod.CheckResult | None, run_mod.CheckResult | None]:
        if keep:  # validity resets the code: keep the fix to look at
            return loop_eval.judge(worktree, rules, repro, sandbox), None
        j = loop_eval.against_key(result, worktree, rules, repro, fix, sandbox)
        return j.judge, j.valid

    return judge


def _refuse_blocked(issue: Issue) -> None:
    """A ticket with an open blocker stops here, before anything is paid."""
    (ticket,) = intake_mod.slices([issue], _blocker_issues([issue]), [])
    if ticket.state == "blocked":
        typer.echo(
            f"#{issue.number} {ticket.reason}: start it once that is merged", err=True
        )
        raise typer.Exit(code=1)


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
    _refuse_blocked(issue)
    refine_mod.update_clone(target, clone_dir(get_settings().clones_dir, target))
    deps, dsn = _flow_deps(target)
    thread = graph_mod.thread_id(issue)
    config = graph_mod.thread_config(thread)
    with graph_mod.compiled(deps, dsn) as graph:
        if graph.get_state(config).values:
            if not restart:
                typer.echo(f"{thread} has a flow: `flow show`, or --restart", err=True)
                raise typer.Exit(code=1)
            graph.checkpointer.delete_thread(thread)  # pyright: ignore[reportOptionalMemberAccess,reportAttributeAccessIssue]
        state = graph_mod.FlowState(issue=issue, route=route)
        graph.invoke(state, config)
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
    config = graph_mod.thread_config(thread)
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
    write_token = write_token_for(target) if apply else None  # before paying
    # the flow's graph in one process: every agent of this run (refine,
    # test, build, review, proofs) shares one budget, the kill switch's cap
    deps, _ = _flow_deps(target)
    steps = Steps(Console(stderr=True, soft_wrap=True))
    with steps.step("clone", f"updating {deps.clone}") as done:
        commit = refine_mod.update_clone(target, deps.clone)
        done(f"{deps.clone} at {commit[:7]}")
    issue = _refresh_issue(target, number, steps)
    if issue is None:
        typer.echo(f"#{number} is not in {settings.issues_dir}", err=True)
        raise typer.Exit(code=1)
    _refuse_blocked(issue)
    state = graph_mod.FlowState(issue=issue, route=route)
    bug = planted_for(issue.title, load_bugs(bugs, seed), seed)
    if bug is not None and bug.kind == "ui" and deps.rules.suite(bug.repro) is None:
        bug = None  # the target has no suite to run a Playwright repro
    if planted and bug is None:
        typer.echo(f"#{number} is not a planted bug the loop can run", err=True)
        raise typer.Exit(code=1)
    if bug is not None:
        role = "handed to the agent" if planted else "judges the result"
        steps.note("repro", f"planted {bug.key} ({bug.repro}) {role}")
        if planted:
            state.planted = str(repro_path(bugs, bug))
        else:  # no verdicts by default: no answer key without a planted bug
            key = _answer_key(repro_path(bugs, bug), fix_path(bugs, bug), keep, deps)
            deps = replace(deps, judge=key)
    if spec is not None:
        given = refine_mod.Refined.model_validate_json(spec.read_text("utf-8"))
        if (given.repo, given.number) != (target, number):
            typer.echo(f"{spec} is for {given.repo}#{given.number}", err=True)
            raise typer.Exit(code=1)
        steps.note("spec", f"from {spec} (at {given.commit[:7]})")
        state.refined, state.route = given, given.route
    deps = replace(deps, keep=keep, prefix="")  # not the flow's worktree
    graph, worktree = graph_mod.in_memory(deps), deps.worktree(issue)
    thread = graph_mod.thread_id(issue)
    if spec is not None:
        now = _invoke(graph, state, thread)
    else:
        with steps.step("refine", f"reading the code ({settings.agent_model})") as done:
            now = _invoke(graph, state, thread)
            if (refined := now.state.refined) is None:
                typer.echo(now.state.note, err=True)
                raise typer.Exit(code=1)
            r = refined.run
            done(f"{r.turns} turns, {r.latency_ms / 1000:.0f} s, ${r.cost_usd:.3f}")
    if now.state.refined is not None:
        show_spec(now.state.refined)
    if now.waiting is None:  # back to the dev: edit the spec (--spec) or the ticket
        typer.echo(now.state.note, err=True)
        raise typer.Exit(code=1)
    while now.waiting is not None:
        answer = _terminal_gate(now, yes)
        if not answer.approve:  # logged for lessons; a rejected test is recorded
            now = _invoke(graph, answer, thread)
            break
        with steps.step("loop", f"repro, fix, checks in {worktree}") as done:
            now = _invoke(graph, answer, thread)
            if (res := now.state.result) is None:
                done("the test written, for the test gate")
            else:
                tries = len(res.attempts)
                done(f"{res.status}, {tries} attempts, ${res.cost_usd:.3f}")
    if not now.state.record:  # the plan rejected: nothing ran
        raise typer.Exit(code=1)
    out = settings.runs_dir / f"{now.state.record}.json"
    record = record_mod.RunRecord.model_validate_json(out.read_text("utf-8"))
    show_loop(
        loop_eval.Judged(result=record.result, judge=record.judge, valid=record.valid)
    )
    if now.state.note:
        typer.echo(now.state.note, err=True)
    if record.gate is not None:
        show_gate(record.gate)
    if record.gate is not None and record.draft is not None:
        body = settings.runs_dir / f"{now.state.record}.pr.md"
        show_draft(record.draft)
        if write_token is None:
            typer.echo(f"-> {body} (dry run: nothing sent to GitHub)")
        else:
            typer.echo(f"-> {body}")
            record.published = _publish(  # a PR or the error: the run is kept
                target,
                deps.clone,
                issue,
                record.gate.result,
                record.draft,
                write_token,
                steps,
            )
            record_mod.write_record(settings.runs_dir, now.state.record, record)
    typer.echo(f"-> {out}")
    blocked = record.gate is not None and record.gate.status == "blocked"
    judged_red = record.judge is not None and not record.judge.ok
    if record.result.status != "green" or blocked or judged_red:
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
