"""Issues as cofabriq stores them: the `IssueSource` seam and its data shape."""

import re
from collections.abc import Iterator
from datetime import date, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

type Closer = Literal["pull_request", "commit"]
type Source = Literal["github", "local"]  # local: written by the dev, never on GitHub

REPO_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*)/[A-Za-z0-9._-]+$")
# a ticket's key as runs, flows, branches and routes name it: `51` or `L3`
KEY = r"^L?(0|[1-9][0-9]*)$"
_KEY = re.compile(KEY)


def ticket_key(source: Source, number: int) -> str:
    """`51` for GitHub's #51, `L3` for the dev's third local ticket."""
    return f"L{number}" if source == "local" else str(number)


def parse_key(key: str) -> tuple[Source, int]:
    """The other way: `"L3"` -> `("local", 3)`, `"51"` -> `("github", 51)`;
    ValueError for anything else (the key becomes part of paths)."""
    if not _KEY.match(key):
        raise ValueError(f"not a ticket key (51 or L3): {key!r}")
    if key.startswith("L"):
        return "local", int(key[1:])
    return "github", int(key)


def key_label(key: str) -> str:
    """How a message names a ticket: `#51` (GitHub's way), `L3` as it is."""
    return key if key.startswith("L") else f"#{key}"


def check_repo(repo: str) -> str:
    """Lowercased `owner/name` (GitHub names are case-insensitive) or ValueError.

    The result becomes a path under `data/`, so `..` and slashes are refused.
    """
    if not REPO_NAME.match(repo) or repo.split("/")[1] in {".", ".."}:
        raise ValueError(f"not an owner/name repo: {repo!r}")
    return repo.lower()


class Comment(BaseModel):
    model_config = ConfigDict(frozen=True)

    author: str | None  # None: deleted account ("ghost")
    bot: bool
    body: str
    created_at: datetime


class IssueRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    number: int


class Issue(BaseModel):
    """One issue with its first comments. Title, body and comments are untrusted.

    `comments` holds the first N only; `comment_count` is the full number.
    `duplicate_of` / `duplicates` are GitHub's own duplicate marks (net of
    unmark events) seen from this issue's timeline, one list per direction.
    `Duplicate of #N` comments are not parsed here: that is the eval set's job.
    `blocked_by` is GitHub's native "blocked by" list (empty for stores
    fetched before the field existed). `closed_by` is what closed it last:
    a merged pull request or a commit that references it, None for a person
    or an open issue (and for stores fetched before the field existed).
    `source` "local": a ticket the dev wrote (`issues/tickets.py`), its own
    numbers (L1, L2, ...); stored issues before it existed are GitHub's.
    """

    model_config = ConfigDict(frozen=True)

    repo: str
    number: int
    source: Source = "github"
    title: str
    body: str
    state: str  # open | closed
    state_reason: str | None  # completed | not_planned | duplicate | reopened
    author: str | None
    labels: list[str]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    closed_by: Closer | None = None
    comment_count: int
    comments: list[Comment]
    duplicate_of: list[IssueRef]  # this issue is a duplicate of these
    duplicates: list[IssueRef]  # these were marked as duplicates of this one
    blocked_by: list[IssueRef] = []  # GitHub's native dependencies

    @property
    def key(self) -> str:
        return ticket_key(self.source, self.number)

    @property
    def label(self) -> str:
        return key_label(self.key)


class IssueSource(Protocol):
    """Yields a repo's issues updated at or after `since`, oldest update first,
    or one issue by number.

    Ascending order makes every prefix of the stream a valid resume point: the
    last `updated_at` seen is the next `since`.
    """

    @property
    def repo(self) -> str: ...

    def issues(self, since: datetime | None = None) -> Iterator[Issue]: ...

    def issue(self, number: int) -> Issue | None: ...

    def created(self, start: date, end: date) -> Iterator[Issue]:
        """Issues created from `start` to `end` (both included), oldest first."""
        ...

    def open_issues(self, limit: int) -> tuple[list[Issue], int]:
        """The newest `limit` open issues, newest first, and the open count."""
        ...
