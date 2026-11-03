"""Commits and files of a local clone, as of a date.

Works on a clone without file contents (`git clone --filter=blob:none`):
only commits and trees are read. Rename detection is off because it needs
the contents; a renamed file simply starts a new history.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from cofabriq.git import git

MARK = "\x1e"  # record separator: never in a path or an ISO date


@dataclass(frozen=True)
class Commit:
    sha: str
    at: datetime
    files: tuple[str, ...]


def rev_at(clone: Path, until: date | None) -> str:
    """The last commit of the checked-out branch at the end of `until` (UTC),
    or its head."""
    args = ["rev-list", "-1"]
    if until is not None:
        args.append(f"--before={until.isoformat()}T23:59:59Z")
    rev = git(clone, *args, "HEAD").strip()
    if not rev:
        raise ValueError(f"no commit in {clone} before {until}")
    return rev


def commits(clone: Path, rev: str) -> Iterator[Commit]:
    """Non-merge commits reachable from `rev`, newest first, with the paths
    they changed. Merge commits are skipped: their changes are the commits
    they merge."""
    out = git(
        clone,
        "log",
        "--no-merges",
        "--no-renames",
        "--name-only",
        f"--format={MARK}%H %cI",
        rev,
    )
    for record in out.split(MARK)[1:]:
        head, *paths = record.strip("\n").split("\n")
        sha, at = head.split(" ")
        yield Commit(sha, datetime.fromisoformat(at), tuple(p for p in paths if p))
