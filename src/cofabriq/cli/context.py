"""The context layer's commands (documents, chunks, vectors, the decision
graph, the MCP server), the service health check and the repo map."""

from collections.abc import Callable
from datetime import date
from pathlib import Path

import psycopg
import typer
from neo4j import GraphDatabase

from cofabriq.cli.common import (
    app,
    embedder,
    map_app,
    neo4j_driver,
    openai_key,
    target_repo,
    triage_fn,
)
from cofabriq.config import get_settings
from cofabriq.context.ingest.chunk import chunk_docs
from cofabriq.context.ingest.extract import CANDIDATES, ExtractedDoc, OpenAIExtractor
from cofabriq.context.ingest.extract import load as load_extracted
from cofabriq.context.ingest.extract import sync as extract_sync
from cofabriq.context.ingest.fetch import fetch as fetch_source
from cofabriq.context.ingest.parse import load_docs
from cofabriq.context.ingest.source import LocalGitSource, Source
from cofabriq.context.store import graph as kg
from cofabriq.context.store import postgres
from cofabriq.git import files_at
from cofabriq.map import build as map_build_mod
from cofabriq.map import components as components_mod
from cofabriq.map import fixes as fixes_mod
from cofabriq.map import history
from cofabriq.map import impact as impact_mod
from cofabriq.map import store as map_store
from cofabriq.mcp_server import build as build_mcp


def _check_postgres() -> str:
    with psycopg.connect(get_settings().postgres_dsn, connect_timeout=3) as conn:
        version = conn.execute("SHOW server_version").fetchone()
    return f"server {version[0] if version else '?'}"


def _check_pgvector() -> str:
    with psycopg.connect(get_settings().postgres_dsn, connect_timeout=3) as conn:
        row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    if row is None:
        raise RuntimeError("extension 'vector' not installed")
    return f"vector {row[0]}"


def _check_neo4j() -> str:
    settings = get_settings()
    auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    with GraphDatabase.driver(settings.neo4j_uri, auth=auth) as driver:
        info = driver.get_server_info()
    return f"{info.agent}"


@app.command()
def health() -> None:
    """Check that Postgres, pgvector and Neo4j are reachable."""
    checks: dict[str, Callable[[], str]] = {
        "postgres": _check_postgres,
        "pgvector": _check_pgvector,
        "neo4j": _check_neo4j,
    }
    failed = False
    for name, check in checks.items():
        try:
            typer.echo(f"{name:<9} OK    {check()}")
        except Exception as exc:
            failed = True
            typer.echo(f"{name:<9} FAIL  {type(exc).__name__}: {exc}")
    raise typer.Exit(code=1 if failed else 0)


@app.command()
def docs(repos: list[Path]) -> None:
    """List the documents cofabriq would ingest from local git clones."""
    source: Source = LocalGitSource(repos)
    count = 0
    for ref in source.refs():
        count += 1
        typer.echo(f"{ref.kind:<9} {ref.sha[:7]}  {ref.repo}/{ref.path}")
    typer.echo(f"{count} documents")


@app.command()
def fetch(repos: list[Path]) -> None:
    """Copy documents from local git clones into the raw dir, skip unchanged."""
    raw_dir = get_settings().raw_dir
    report = fetch_source(LocalGitSource(repos), raw_dir)
    typer.echo(
        f"{report.written} written, {report.unchanged} unchanged, "
        f"{report.removed} removed -> {raw_dir}"
    )


@map_app.command("build")
def map_build(clone: Path, repo: str | None = None, until: str | None = None) -> None:
    """Build the repo map from a local clone's history into Neo4j, replacing
    that repo's map. `--until 2022-12-31`: the map as of that day (evals must
    not see the fixes they predict). A clone without file contents is
    enough: `git clone --filter=blob:none --no-checkout <url>`."""
    target = target_repo(repo)
    day = date.fromisoformat(until) if until else None
    rules = components_mod.load_rules(get_settings().targets_dir, target)
    rev = history.rev_at(clone, day)
    m = map_build_mod.build(files_at(clone, rev), history.commits(clone, rev), rules)
    fixed = fixes_mod.fixes(clone, rev, target)
    rows = map_store.fix_rows(fixed, rules, set(m.files.values()))
    with neo4j_driver() as driver:
        report = map_store.write(driver, target, rev, day, m, rows)
    typer.echo(
        f"{target} at {rev[:10]} ({until or 'head'}): {report.components} "
        f"components, {report.files} files; {m.used} commits counted, "
        f"{m.skipped} skipped (over {map_build_mod.MAX_FILES} files); "
        f"{report.component_pairs} component pairs, {report.file_pairs} file "
        f"pairs, {report.fixes} fix commits"
    )


@map_app.command("show")
def map_show(
    repo: str | None = None, component: str | None = None, k: int = 10
) -> None:
    """The map's components (most changed first), or one component's
    strongest co-change partners."""
    target = target_repo(repo)
    with neo4j_driver() as driver:
        if component is None:
            for c in map_store.components(driver, target)[:k]:
                typer.echo(f"{c.commits:>6} commits  {c.files:>5} files  {c.name}")
            return
        for p in map_store.changes_with(driver, target, component, k):
            typer.echo(f"{p.n:>6} together  {p.share:>5.0%}  {p.name}")


@app.command()
def chunks(full: bool = False) -> None:
    """Parse and chunk the raw dir; print one line per chunk (or full text)."""
    count = 0
    docs: set[tuple[str, str]] = set()
    for c in chunk_docs(load_docs(get_settings().raw_dir)):
        count += 1
        docs.add((c.repo, c.path))
        where = f"{c.repo}/{c.path}#{c.ord}"
        typer.echo(f"{c.kind:<9} {len(c.text):>5}  {where}  {c.heading}")
        if full:
            typer.echo(f"{c.text}\n")
    typer.echo(f"{count} chunks from {len(docs)} documents")


@app.command()
def embed() -> None:
    """Embed the raw dir's chunks into Postgres; skip documents already stored."""
    settings = get_settings()
    chunked = chunk_docs(load_docs(settings.raw_dir))
    with postgres.connect(settings.postgres_dsn) as conn:
        report = postgres.sync(conn, chunked, embedder())
    typer.echo(
        f"{report.embedded} documents embedded ({report.chunks} chunks), "
        f"{report.unchanged} unchanged, {report.removed} removed"
    )


@app.command()
def search(query: str, k: int = 5, full: bool = False) -> None:
    """Semantic search over the embedded chunks, best match first."""
    [vector] = embedder()([query])
    with postgres.connect(get_settings().postgres_dsn) as conn:
        hits = postgres.search(conn, vector, k)
    for h in hits:
        typer.echo(f"{h.score:.3f}  {h.kind:<9} {h.repo}/{h.path}#{h.ord}  {h.heading}")
        if full:
            typer.echo(f"{h.text}\n")


def _show(record: ExtractedDoc) -> None:
    e = record.extraction
    typer.echo(f"{record.ref.kind:<9} {record.ref.repo}/{record.ref.path}")
    if e.status:
        typer.echo(f"  status        {e.status}")
    for name, values in [
        ("technologies", e.technologies),
        ("teams", e.teams),
        ("revises", [link.target for link in e.revises]),
        ("revisits", [link.target for link in e.revisits]),
        ("depends_on", [link.target for link in e.depends_on]),
        ("affects", e.affects),
    ]:
        if values:
            typer.echo(f"  {name:<13} {', '.join(values)}")
    for reason in record.dropped:
        typer.echo(f"  dropped       {reason}")


@app.command()
def extract() -> None:
    """Fill the domain schema per document with the LLM; skip unchanged ones.

    Needs `embed` first: revision candidates come from the vector store.
    """
    settings = get_settings()
    docs = load_docs(settings.raw_dir)
    with postgres.connect(settings.postgres_dsn) as conn:
        report = extract_sync(
            docs,
            settings.extracted_path,
            OpenAIExtractor(openai_key()),
            lambda ref: postgres.similar_adrs(
                conn, (ref.repo, ref.path, ref.sha), CANDIDATES
            ),
            on_extracted=_show,
        )
    typer.echo(
        f"{report.extracted} documents extracted, {report.unchanged} unchanged, "
        f"{report.removed} removed, {report.dropped} links dropped "
        f"-> {settings.extracted_path}"
    )


@app.command()
def graph() -> None:
    """Load the extraction records into Neo4j; skip documents already there."""
    settings = get_settings()
    docs = load_docs(settings.raw_dir)
    records = load_extracted(settings.extracted_path)
    with neo4j_driver() as driver:
        report = kg.sync(driver, docs, records)
    typer.echo(
        f"{report.written} documents written, {report.unchanged} unchanged, "
        f"{report.removed} removed"
    )


@app.command()
def decisions(project: str | None = None) -> None:
    """List ADRs: in force, or what revises them; plus where they're revisited."""
    with neo4j_driver() as driver:
        found = kg.decisions(driver, project)
    for d in found:
        verdict = "in force" if d.in_force else (d.status or "revised")
        typer.echo(f"{verdict:<11} {d.date or '?':<10}  {d.id}")
        for label, ids in [
            ("revised by", d.revised_by),
            ("revisits", d.revisits),
            ("revisited by", d.revisited_by),
        ]:
            for i in ids:
                typer.echo(f"{'':<23}  {label} {i}")
    in_force = sum(d.in_force for d in found)
    typer.echo(f"{in_force} of {len(found)} decisions in force")


@app.command()
def affected(adr: str) -> None:
    """Projects a decision touches, directly or through `depends_on` (repo:path)."""
    with neo4j_driver() as driver:
        found = kg.affected(driver, adr)
    if not found:
        typer.echo(f"no affected projects for {adr} (id is repo:path)", err=True)
        raise typer.Exit(code=1)
    for a in found:
        how = "named" if a.hops == 0 else f"{a.hops} hop{'s' * (a.hops > 1)}"
        typer.echo(f"{how:<8} {a.project}")


@app.command()
def mcp() -> None:
    """Serve search, decisions, affected, triage and impact as MCP tools
    over stdio."""
    embed = embedder()
    with (
        postgres.connect(get_settings().postgres_dsn) as conn,
        neo4j_driver() as driver,
    ):
        # Long-lived: one transaction per query, none left open. Writes only
        # when `triage` refreshes an issue (its own transaction).
        conn.autocommit = True
        target = get_settings().target_repo
        run = triage_fn(conn, refresh=True)
        impact_run = impact_mod.impact_fn(
            conn, driver, embed, get_settings().issues_dir
        )
        build_mcp(
            embed, conn, driver, run, default_repo=target, impact_fn=impact_run
        ).run("stdio")
