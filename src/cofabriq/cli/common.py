"""The typer apps and what several command modules share: the target
repo, the GitHub source, the target's loop rules, `--only`, the agent, the
write token and the OpenAI and Neo4j clients."""

from collections.abc import Callable, Collection

import typer
from neo4j import Driver

from cofabriq.config import get_settings
from cofabriq.context.ingest.embed import Embed, openai_embedder
from cofabriq.context.store import graph as kg
from cofabriq.context.store import postgres
from cofabriq.issues import rerank as rerank_mod
from cofabriq.issues import triage as triage_mod
from cofabriq.issues import verdict as verdict_mod
from cofabriq.issues.github import GitHubIssueSource
from cofabriq.issues.model import check_repo
from cofabriq.loop import publish as publish_mod
from cofabriq.loop import run as run_mod
from cofabriq.loop.engine import ApiKey, Auth, ClaudeCode, Subscription
from cofabriq.steps import Steps

app = typer.Typer(no_args_is_help=True)
eval_app = typer.Typer(no_args_is_help=True, help="Measure retrieval and verdicts.")
app.add_typer(eval_app, name="eval")
map_app = typer.Typer(no_args_is_help=True, help="The repo map: components, co-change.")
app.add_typer(map_app, name="map")
flow_app = typer.Typer(
    no_args_is_help=True,
    help="`run` as a LangGraph graph that pauses at its gates; "
    "`serve --write` answers them too.",
)
app.add_typer(flow_app, name="flow")


@app.callback()
def main() -> None:
    """A software factory pointed at a GitHub repo: triage, refine and the
    core loop for small issues, the repo map and its context for planning."""


def target_repo(repo: str | None) -> str:
    target = repo or get_settings().target_repo
    if target is None:
        typer.echo("pass --repo or set TARGET_REPO (.env.example)", err=True)
        raise typer.Exit(code=1)
    return check_repo(target)


def github(repo: str) -> GitHubIssueSource | None:
    """The read-only GitHub source for `repo`; None without GITHUB_TOKEN."""
    settings = get_settings()
    if settings.github_token is None:
        return None
    token = settings.github_token.get_secret_value()
    return GitHubIssueSource(repo, token, comments=settings.issue_comments)


def loop_rules(target: str) -> run_mod.LoopRules:
    """The target file's `[loop]`; exits when it has none."""
    rules = run_mod.load_rules(get_settings().targets_dir, target)
    if rules is None:
        typer.echo(f"no [loop] in the target file of {target}", err=True)
        raise typer.Exit(code=1)
    return rules


def pick_only[T](
    items: list[T],
    only: str | None,
    key: Callable[[T], str],
    what: str,
    known: Collection[str] = (),
) -> list[T]:
    """The items `--only key,key` picks, all without it; exits on a key
    none of them has, so a typo doesn't run nothing. `known`: real keys
    that may have no item (a bug without a case), told apart from typos."""
    if only is None:
        return items
    keys = set(only.split(","))
    missing = keys - {key(i) for i in items}
    no_item = {k for k in missing if k in known}
    unknown = missing - no_item
    if no_item:
        typer.echo(f"no case for: {', '.join(sorted(no_item))}", err=True)
    if unknown:
        typer.echo(f"unknown {what} keys: {', '.join(sorted(unknown))}", err=True)
    if missing:
        raise typer.Exit(code=1)
    return [i for i in items if key(i) in keys]


def triage_fn(
    conn: postgres.Conn, *, refresh: bool, steps: Steps | None = None
) -> Callable[[str, int], triage_mod.TriageCard]:
    """`triage_mod.triage_fn` with the OpenAI clients and the settings.
    `refresh` needs `GITHUB_TOKEN`; without one the store is used as it is.
    The eval passes False: it scores the issues as fetched. `steps` None
    stays silent (MCP, eval)."""
    settings = get_settings()
    key = openai_key()
    return triage_mod.triage_fn(
        conn,
        issues_dir=settings.issues_dir,
        targets_dir=settings.targets_dir,
        triager=triage_mod.OpenAITriager(key),
        judge=verdict_mod.OpenAIJudge(key),
        reranker=rerank_mod.OpenAIReranker(key) if settings.rerank_candidates else None,
        embed=openai_embedder(key),
        github=github if refresh else None,
        steps=steps or Steps(None),
    )


def claude_code() -> ClaudeCode:
    settings = get_settings()
    auth: Auth
    if settings.agent_auth == "subscription":
        if settings.claude_code_oauth_token is None:
            typer.echo(
                "CLAUDE_CODE_OAUTH_TOKEN is not set (`claude setup-token`)", err=True
            )
            raise typer.Exit(code=1)
        auth = Subscription(settings.claude_code_oauth_token.get_secret_value())
        typer.echo("agent: your Claude plan; costs shown are API list prices", err=True)
    elif settings.anthropic_api_key is None:
        typer.echo("ANTHROPIC_API_KEY is not set (see .env.example)", err=True)
        raise typer.Exit(code=1)
    else:
        auth = ApiKey(settings.anthropic_api_key.get_secret_value())
    return ClaudeCode(
        auth=auth,
        model=settings.agent_model,
        effort=settings.agent_effort,
        stop_file=settings.stop_file,
    )


def write_token_for(repo: str) -> str:
    """The write token for `--apply`, after the repo check."""
    try:
        publish_mod.check_writable(repo)
    except publish_mod.PublishError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    token = get_settings().github_write_token
    if token is None:
        typer.echo("GITHUB_WRITE_TOKEN is not set (see .env.example)", err=True)
        raise typer.Exit(code=1)
    return token.get_secret_value()


def openai_key() -> str:
    key = get_settings().openai_api_key
    if key is None:
        typer.echo("OPENAI_API_KEY is not set (see .env.example)", err=True)
        raise typer.Exit(code=1)
    return key.get_secret_value()


def embedder() -> Embed:
    return openai_embedder(openai_key())


def neo4j_driver() -> Driver:
    settings = get_settings()
    return kg.connect(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password.get_secret_value(),
    )
