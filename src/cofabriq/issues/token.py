"""Which GitHub token cofabriq reads with, and whether it could write.

cofabriq only reads GitHub (issues, their blockers, its own draft PRs). The
token comes from `GITHUB_TOKEN` first: a fine-grained token with Issues: read
and Metadata: read is all it needs. `gh auth token` (the GitHub CLI's login)
only when the dev opts in (`GITHUB_TOKEN_FROM_GH=true`): that token usually
carries the `repo` scope, which can write, so cofabriq says so.

Only classic and OAuth tokens (`gh`'s is one) list their scopes, in the
`X-OAuth-Scopes` response header. A fine-grained token sends none: its
permissions can't be read from outside, so no warning either way.
"""

import subprocess
from collections.abc import Callable
from typing import Literal

import httpx
from pydantic import BaseModel

API = "https://api.github.com"
# a classic scope that can write issues, code or PRs of a repo
WRITE_SCOPES = frozenset({"repo", "public_repo"})

type TokenSource = Literal["env", "gh"]
type Run = Callable[..., subprocess.CompletedProcess[str]]


class Access(BaseModel):
    """The token as the dev should know it; never the token itself."""

    source: TokenSource | None  # None: no token, so no import
    scopes: list[str] | None  # None: not listed (fine-grained) or not checked
    can_write: bool | None  # None: can't tell
    warning: str


def gh_token(run: Run = subprocess.run) -> str | None:
    """The GitHub CLI's token, or None (no `gh`, not logged in)."""
    try:
        done = run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None


def resolve(
    env: str | None, from_gh: bool, gh: Callable[[], str | None] = gh_token
) -> tuple[str | None, TokenSource | None]:
    """`GITHUB_TOKEN` if set, else `gh auth token` if opted in, else none."""
    if env:
        return env, "env"
    if from_gh and (found := gh()) is not None:
        return found, "gh"
    return None, None


def scopes(
    token: str, transport: httpx.BaseTransport | None = None
) -> list[str] | None:
    """The token's classic scopes from one read (`GET /`); None when the
    response lists none (a fine-grained token) or GitHub can't be reached."""
    try:
        with httpx.Client(base_url=API, timeout=10, transport=transport) as client:
            resp = client.get("/", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        return None
    header = resp.headers.get("x-oauth-scopes")
    if header is None:
        return None
    return sorted(s.strip() for s in header.split(",") if s.strip())


def access(source: TokenSource | None, listed: list[str] | None) -> Access:
    if source is None:
        return Access(source=None, scopes=None, can_write=None, warning="")
    if listed is None:
        return Access(source=source, scopes=None, can_write=None, warning="")
    writes = sorted(WRITE_SCOPES & set(listed))
    warning = (
        f"this token can write to your repositories (scope {', '.join(writes)}); "
        "cofabriq only reads, so a fine-grained token with Issues: read and "
        "Metadata: read in GITHUB_TOKEN is safer"
        if writes
        else ""
    )
    return Access(source=source, scopes=listed, can_write=bool(writes), warning=warning)
