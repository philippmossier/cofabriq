"""Issue ingest, the triage card and impact for one issue."""

from datetime import date
from pathlib import Path

import typer
from rich.console import Console

from cofabriq.cli.common import (
    app,
    embedder,
    github,
    github_access,
    neo4j_driver,
    openai_key,
    target_repo,
    ticket_app,
    triage_fn,
)
from cofabriq.cli.render import show_card
from cofabriq.config import get_settings
from cofabriq.context.store import postgres
from cofabriq.issues import tickets as tickets_mod
from cofabriq.issues import triage as triage_mod
from cofabriq.issues import vectors
from cofabriq.issues.fetch import fetch_created, fetch_issues, fetch_open, load_issues
from cofabriq.loop import onboard as onboard_mod
from cofabriq.map import impact as impact_mod
from cofabriq.steps import Steps


def _date_range(value: str) -> tuple[date, date]:
    """`2022-01-01..2024-12-31`, GitHub search syntax."""
    try:
        start, end = (date.fromisoformat(part) for part in value.split(".."))
    except ValueError:
        msg = f"expected YYYY-MM-DD..YYYY-MM-DD, got {value!r}"
        raise typer.BadParameter(msg) from None
    if start > end:
        raise typer.BadParameter(f"{start} is after {end}")
    return start, end


@app.command("fetch-issues")
def fetch_issues_cmd(
    repo: str | None = None,
    created: str | None = None,
    open: bool = False,
    limit: int = 50,
) -> None:
    """Copy a GitHub repo's issues into the issues dir, only what changed.

    `--created 2022-01-01..2024-12-31` fetches the issues created in that
    window instead (eval sets on large repos); the update cursor is untouched.
    `--open`: only the newest `--limit` open issues (at most 100), for a repo
    you just added; says how many more there are.
    """
    settings = get_settings()
    target = target_repo(repo)
    if not onboard_mod.on_github(settings.targets_dir, target):
        typer.echo(f"{target} is not on github.com: write local tickets", err=True)
        raise typer.Exit(code=1)
    source = github(target)
    if source is None:
        typer.echo("no GitHub token: set GITHUB_TOKEN (.env.example)", err=True)
        raise typer.Exit(code=1)
    if warning := github_access().warning:
        typer.echo(f"warning: {warning}", err=True)
    if open:
        try:
            got = fetch_open(source, settings.issues_dir, limit)
        except ValueError as exc:  # the limit
            raise typer.BadParameter(str(exc)) from exc
        more = got.total - got.written - got.unchanged
        typer.echo(
            f"{got.written} written, {got.unchanged} unchanged, newest open first; "
            f"{got.total} open in all{f', {more} not imported' if more > 0 else ''} "
            f"-> {settings.issues_dir}"
        )
        return
    if created is not None:
        start, end = _date_range(created)
        report = fetch_created(source, settings.issues_dir, start, end)
        typer.echo(
            f"{report.written} written, {report.unchanged} unchanged "
            f"(created {start}..{end}) -> {settings.issues_dir}"
        )
        return
    report = fetch_issues(source, settings.issues_dir)
    since = report.since.isoformat() if report.since else "the start"
    typer.echo(
        f"{report.written} written, {report.unchanged} unchanged "
        f"(updated since {since}) -> {settings.issues_dir}"
    )


@app.command("embed-issues")
def embed_issues(repo: str | None = None) -> None:
    """Embed fetched issues (title + body) into Postgres; skip unchanged ones."""
    settings = get_settings()
    issues = load_issues(settings.issues_dir, target_repo(repo))
    with postgres.connect(settings.postgres_dsn) as conn:
        report = vectors.sync(conn, issues, embedder())
    typer.echo(f"{report.embedded} issues embedded, {report.unchanged} unchanged")


@ticket_app.command("new")
def ticket_new(
    title: str,
    body: Path | None = None,
    repo: str | None = None,
    anyway: bool = False,
) -> None:
    """Write a local ticket (`L1`, `L2`, ...) for `run` and `flow start`;
    `--body` is a file with its text. One LLM call reads it first and says
    what is missing (it fills nothing in); with gaps, it is saved only with
    `--anyway`."""
    settings = get_settings()
    target = target_repo(repo)
    text = body.read_text("utf-8") if body is not None else ""
    components = triage_mod.load_components(settings.targets_dir, target)
    triager = triage_mod.OpenAITriager(openai_key())
    try:
        found = tickets_mod.check(target, title, text, components, triager)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"reads as a {found.type} (${found.cost_usd:.4f})")
    for line in found.missing:
        typer.echo(f"  - {line}")
    if found.missing and not anyway:
        typer.echo("not saved: add what is missing, or --anyway", err=True)
        raise typer.Exit(code=1)
    ticket = tickets_mod.new_ticket(settings.tickets_dir, target, title, text)
    folder = tickets_mod.ticket_dir(settings.tickets_dir, target)
    typer.echo(f"{ticket.key} -> {folder / f'{ticket.key}.json'}")
    typer.echo(
        f"-> cofabriq flow start {ticket.key} --repo {target} --type {found.type}"
    )


@app.command()
def triage(number: int, repo: str | None = None, json: bool = False) -> None:
    """Triage card for one issue (two LLM calls). Fetches and embeds the issue
    first if it is new or changed on GitHub (needs GITHUB_TOKEN)."""
    target = target_repo(repo)
    with postgres.connect(get_settings().postgres_dsn) as conn:
        try:
            steps = Steps(Console(stderr=True, soft_wrap=True))
            card = triage_fn(conn, refresh=True, steps=steps)(target, number)
        except triage_mod.NotReady as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    if json:
        typer.echo(card.model_dump_json(indent=2))
    else:
        show_card(card)


@app.command()
def impact(
    number: int | None = typer.Argument(None),
    plan: Path | None = None,
    repo: str | None = None,
    k: int = 8,
    json: bool = False,
) -> None:
    """Components a fix for issue <number> (or a plan, `--plan plan.md`) will
    likely touch: earlier similar issues vote with what their fixes touched,
    co-change on the map adds the blast radius. Needs `map build`."""
    if (number is None) == (plan is None):
        typer.echo("pass an issue number or --plan, not both", err=True)
        raise typer.Exit(code=1)
    target = target_repo(repo)
    text = plan.read_text("utf-8") if plan else None
    settings = get_settings()
    with neo4j_driver() as driver, postgres.connect(settings.postgres_dsn) as conn:
        try:
            run = impact_mod.impact_fn(conn, driver, embedder(), settings.issues_dir)
            result = run(target, number, text, k)
        except triage_mod.NotReady as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    if json:
        typer.echo(result.model_dump_json(indent=2))
        return
    subject = f"#{number}" if number is not None else str(plan)
    typer.echo(f"{target} {subject}, map at {result.map_rev[:10]}")
    if not result.components:
        typer.echo("  no earlier issue with a fix on the map: no evidence to go on")
    for t in result.components:
        why = ", ".join(f"#{n}" for n in t.via[:4])
        typer.echo(f"  {t.score:>6.2f}  {t.component}  (fixes of {why})")
    if result.blast:
        typer.echo("  usually changes with them (blast radius):")
    for t in result.blast:
        typer.echo(f"  {t.score:>6.2f}  {t.component}")
