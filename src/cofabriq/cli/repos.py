"""Your own repo: `repo add` proposes its `[loop]` from its files, `repo
check` proves it in the sandbox. Neither needs a GitHub token: the code
comes from the dev's local clone."""

from pathlib import Path

import typer
from rich.console import Console

from cofabriq.cli.common import claude_code, repo_app
from cofabriq.config import get_settings
from cofabriq.issues.model import check_repo
from cofabriq.loop import onboard
from cofabriq.loop import refine as refine_mod
from cofabriq.loop import run as run_mod
from cofabriq.loop.engine import AgentError, Guard, Guarded
from cofabriq.loop.repo_jobs import RepoJobs
from cofabriq.loop.sandbox import Podman
from cofabriq.steps import Steps
from cofabriq.targets import clone_dir, own_target, repo_slug, target_file

PROPOSE_BUDGET_USD = 1.0


@repo_app.command("add")
def repo_add(path: Path, force: bool = False) -> None:
    """Propose `[loop]` for the repo cloned at PATH: a read-only agent reads
    its manifests, Makefile and CI (paid, one agent run). Writes
    targets/own/<owner>-<name>.toml for you to read and edit; nothing runs
    until `repo check`."""
    settings = get_settings()
    steps = Steps(Console(stderr=True, soft_wrap=True))
    try:
        source = onboard.clone_source(path.absolute())
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    repo = onboard.identity(source.path)
    out = own_target(settings.targets_dir, repo)
    if out.is_file() and not force:
        typer.echo(f"{out} exists: edit it, or --force to propose again", err=True)
        raise typer.Exit(code=1)
    behind = refine_mod.behind(source)
    if behind:
        steps.note(
            "base",
            f"{source.branch} is {behind} commit(s) behind origin/{source.branch}"
            " (as of your last fetch): runs start from your local branch",
            ok=False,
        )
    clone = clone_dir(settings.clones_dir, repo)
    with steps.step("clone", f"{repo} from {source.url}") as done:
        commit = refine_mod.sync_clone(clone, source.url, f"refs/heads/{source.branch}")
        done(f"{source.branch} at {commit[:7]} in {clone}")
    agent = Guarded(claude_code(), Guard(PROPOSE_BUDGET_USD))
    with steps.step("agent", f"reading how it builds ({agent.model})") as done:
        try:
            proposed = onboard.propose(repo, clone, agent)
        except AgentError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        r = proposed.run
        done(f"{r.turns} turns, {r.latency_ms / 1000:.0f} s, ${r.cost_usd:.3f}")
    unchecked = [c.field for c in proposed.unchecked]
    found = len(proposed.citations) - len(unchecked)
    detail = f"{found} of {len(proposed.citations)} quoted from the repo"
    if unchecked:
        detail += f"; unchecked: {', '.join(unchecked)}"
    steps.note("sources", detail, ok=not unchecked)
    onboard.save_proposal(settings.targets_dir, proposed, source)
    typer.echo(f"-> {out}")
    typer.echo(f"read it, then: uv run cofabriq repo check {repo}")


@repo_app.command("check")
def repo_check(repo: str) -> None:
    """Prove REPO's `[loop]` on its base commit in the sandbox: install,
    every check green, and each suite's canary red when it fails and green
    when it passes (free, minutes). The proof goes next to the target file;
    editing the file takes it back."""
    settings = get_settings()
    target = check_repo(repo)
    steps = Steps(Console(stderr=True, soft_wrap=True))
    path = target_file(settings.targets_dir, target)
    rules = run_mod.load_rules(settings.targets_dir, target)
    if path is None or rules is None:
        typer.echo(f"no [loop] for {target}: uv run cofabriq repo add <path>", err=True)
        raise typer.Exit(code=1)
    source = refine_mod.clone_from(settings.targets_dir, target)
    if source is not None and (behind := refine_mod.behind(source)):
        steps.note(
            "base",
            f"{source.branch} is {behind} commit(s) behind origin/{source.branch}",
            ok=False,
        )
    what = f"updating the clone, install, {len(rules.checks)} checks, canaries"
    with steps.step("check", what) as done:
        checked = prove(target)
        done(f"{checked.seconds:.0f} s")
    show_checked(checked, steps)
    typer.echo(f"-> {onboard.proof_path(settings.targets_dir, target)}")
    if not checked.proven:
        typer.echo("not proven: fix the target file and check again", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"proven: {target} is ready for the loop (`serve` lists it)")


def prove(repo: str, on_step: onboard.OnStep | None = None) -> onboard.Checked:
    """`repo check`'s work: cofabriq's clone updated from the dev's branch,
    the check on its tip, the proof saved next to the target file."""
    settings = get_settings()
    path = target_file(settings.targets_dir, repo)
    rules = run_mod.load_rules(settings.targets_dir, repo)
    if path is None or rules is None:
        raise ValueError(f"no [loop] for {repo}")
    clone = clone_dir(settings.clones_dir, repo)
    commit = refine_mod.update_clone(repo, clone, targets_dir=settings.targets_dir)
    checked = onboard.check(
        repo,
        rules,
        config=onboard.config_hash(path),
        clone=clone,
        commit=commit,
        worktree=settings.work_dir / f"{repo_slug(repo)}-check",
        sandbox=Podman(image=rules.image, setup=rules.setup),
        on_step=on_step,
    )
    onboard.save_proof(settings.targets_dir, checked)
    return checked


def propose_at(path: Path, earlier: onboard.Repair | None) -> onboard.Proposed:
    """`repo add`'s work for the browser: cofabriq's clone from the dev's
    clone at `path`, the proposal (told how the last one failed, with
    `earlier`), both files saved."""
    settings = get_settings()
    source = onboard.clone_source(path)
    repo = onboard.identity(source.path)
    clone = clone_dir(settings.clones_dir, repo)
    refine_mod.sync_clone(clone, source.url, f"refs/heads/{source.branch}")
    agent = Guarded(claude_code(), Guard(PROPOSE_BUDGET_USD))
    proposed = onboard.propose(repo, clone, agent, earlier)
    onboard.save_proposal(settings.targets_dir, proposed, source)
    return proposed


def setups() -> RepoJobs:
    """The browser's "Add a repo": `repo add` and `repo check` as jobs."""
    return RepoJobs(get_settings().targets_dir, propose_at, prove)


def show_checked(checked: onboard.Checked, steps: Steps) -> None:
    """One line per step of the check, the failing output under it."""
    results = [checked.install, *checked.checks]
    for r in results:
        steps.note(r.name, f"{r.seconds:.0f} s" if r.ok else "failed", ok=r.ok)
        if not r.ok:
            typer.echo(_indented(r.output), err=True)
    for c in checked.canaries:
        red = "red when failing" if c.red else "NOT red when failing"
        green = "green when passing" if c.green else "NOT green when passing"
        steps.note(f"canary {c.suite}", f"{red}, {green}", ok=c.red and c.green)
        if c.output:
            typer.echo(_indented(c.output), err=True)


def _indented(text: str, lines: int = 20) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines()[-lines:])
