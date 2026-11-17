"""Local tickets: written by the dev (`ticket new`, the UI's "Write a
ticket"), never on GitHub. Each one is an `Issue` with `source = "local"` in
`<tickets_dir>/<owner>/<name>/L<n>.json`, numbered per repo from L1. Apart
from GitHub's store on purpose: no index, no cursor, never embedded, so
`fetch-issues` and the duplicate search never see them.

Before a ticket is saved, the triage card's field call reads it and says
what is missing (`check`); it never fills anything in, and the dev may save
anyway: `intake` and the spec's readiness check still gate the run.
"""

import os
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from cofabriq import llm
from cofabriq.files import write_atomic
from cofabriq.issues import readiness
from cofabriq.issues import triage as triage_mod
from cofabriq.issues.fetch import issue_dir, load_issue
from cofabriq.issues.model import Issue, check_repo, parse_key
from cofabriq.issues.triage import IssueType, Triager

_FILE = re.compile(r"^L([1-9][0-9]*)\.json$")
MAX_TITLE = 200
MAX_BODY = 20_000


def ticket_dir(tickets_dir: Path, repo: str) -> Path:
    return issue_dir(tickets_dir, repo)


def _numbers(folder: Path) -> list[int]:
    if not folder.is_dir():
        return []
    found = (_FILE.match(p.name) for p in folder.iterdir())
    return sorted(int(m.group(1)) for m in found if m)


def cleaned(title: str, body: str) -> tuple[str, str]:
    """Title and text stripped, or ValueError: no title, or too long."""
    title, body = title.strip(), body.strip()
    if not title:
        raise ValueError("a ticket needs a title")
    if len(title) > MAX_TITLE or len(body) > MAX_BODY:
        raise ValueError(f"title up to {MAX_TITLE} characters, text up to {MAX_BODY}")
    return title, body


def new_ticket(
    tickets_dir: Path, repo: str, title: str, body: str, now: datetime | None = None
) -> Issue:
    """Store a new open ticket under the next free number. The file is
    created with O_EXCL: two saves at once can't both take L3, the second
    one moves on to L4."""
    title, body = cleaned(title, body)
    folder = ticket_dir(tickets_dir, repo)
    folder.mkdir(parents=True, exist_ok=True)
    at = now or datetime.now(UTC)
    number = max(_numbers(folder), default=0) + 1
    while True:
        issue = _ticket(check_repo(repo), number, title, body, at)
        try:
            fd = os.open(
                folder / f"L{number}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL
            )
        except FileExistsError:  # taken since the listing
            number += 1
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(issue.model_dump_json(indent=2))
        return issue


def _ticket(repo: str, number: int, title: str, body: str, at: datetime) -> Issue:
    return Issue(
        repo=repo,
        number=number,
        source="local",
        title=title,
        body=body,
        state="open",
        state_reason=None,
        author=None,
        labels=[],
        created_at=at,
        updated_at=at,
        closed_at=None,
        comment_count=0,
        comments=[],
        duplicate_of=[],
        duplicates=[],
    )


def load_ticket(tickets_dir: Path, repo: str, number: int) -> Issue | None:
    path = ticket_dir(tickets_dir, repo) / f"L{number}.json"
    return Issue.model_validate_json(path.read_bytes()) if path.is_file() else None


def close_ticket(
    tickets_dir: Path, repo: str, number: int, now: datetime | None = None
) -> tuple[Issue, bool] | None:
    """Mark a ticket closed as completed. None when there is no such ticket;
    otherwise the ticket and whether this call closed it (False: it was
    closed already, the file untouched)."""
    ticket = load_ticket(tickets_dir, repo, number)
    if ticket is None:
        return None
    if ticket.state == "closed":
        return ticket, False
    at = now or datetime.now(UTC)
    closed = ticket.model_copy(
        update={
            "state": "closed",
            "state_reason": "completed",
            "closed_at": at,
            "updated_at": at,
        }
    )
    path = ticket_dir(tickets_dir, repo) / f"L{number}.json"
    write_atomic(path, closed.model_dump_json(indent=2).encode())
    return closed, True


def load_tickets(tickets_dir: Path, repo: str) -> list[Issue]:
    folder = ticket_dir(tickets_dir, repo)
    return [t for n in _numbers(folder) if (t := load_ticket(tickets_dir, repo, n))]


def find(issues_dir: Path, tickets_dir: Path, repo: str, key: str) -> Issue | None:
    """The stored ticket a key names: `L3` from the local store, `51` from
    GitHub's. ValueError for a key that is neither."""
    source, number = parse_key(key)
    if source == "local":
        return load_ticket(tickets_dir, repo, number)
    return load_issue(issues_dir, repo, number)


class TicketCheck(BaseModel):
    """What the readiness read says about a draft ticket."""

    type: IssueType
    missing: list[str]  # one line per gap, most important first; empty: ready
    cost_usd: float


def check(
    repo: str, title: str, body: str, components: dict[str, str], triager: Triager
) -> TicketCheck:
    """The triage card's field call on the draft (no duplicate search: a
    local ticket has no earlier issues to match), then the same checks:
    required info with quotes verified against the text, blocking questions.
    The draft as an issue numbered 0: not stored, nothing to collide with.
    ValueError before any call for a draft `cleaned` refuses."""
    title, body = cleaned(title, body)
    draft = _ticket(check_repo(repo), 0, title, body, datetime.now(UTC))
    raw, usage = triager.fields(draft, components)
    fields, _ = triage_mod.check(raw, components)
    items, _ = readiness.check_info(fields.type, fields.info, triage_mod.shown(draft))
    missing: list[str] = []
    if fields.type not in ("bug", "feature"):
        missing.append(f"reads as a {fields.type}: cofabriq takes bugs and features")
    missing += [f"missing {i}: {readiness.ITEMS[i]}" for i in items]
    if fields.blocking:
        missing += [f"open question: {q}" for q in fields.missing_info]
    return TicketCheck(type=fields.type, missing=missing, cost_usd=llm.cost(usage))
