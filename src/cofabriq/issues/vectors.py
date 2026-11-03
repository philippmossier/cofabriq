"""Issue vectors in pgvector: one row per issue, nearest earlier issues."""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pgvector import Vector
from psycopg import sql

from cofabriq.context.ingest.embed import DIM, Embed
from cofabriq.context.store.postgres import Conn
from cofabriq.issues.model import Issue

MAX_BODY = 6000  # characters; bodies with pasted logs reach 50k+

# No ANN index: a few thousand issues per repo scan exactly in milliseconds,
# and an HNSW scan with a `created_at` filter can return fewer than k rows.
SCHEMA = sql.SQL("""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS issues (
    repo       text        NOT NULL,
    number     int         NOT NULL,
    created_at timestamptz NOT NULL,
    input_sha  text        NOT NULL,
    embedding  vector({dim}) NOT NULL,
    PRIMARY KEY (repo, number)
);
""").format(dim=sql.Literal(DIM))


@dataclass(frozen=True)
class IssueSyncReport:
    embedded: int
    unchanged: int


@dataclass(frozen=True)
class Neighbour:
    number: int
    score: float  # cosine similarity, 1 = same direction


FENCE_HEAD = 600  # characters kept from the start of a code fence
FENCE_TAIL = 200  # and from its end: a traceback names the error last

_FENCE = re.compile(r"```[^\n]*\n.*?```", re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
# The blocks vscode's issue reporter appends; a dev's own <details> stays.
_REPORTER_DETAILS = re.compile(
    r"<details>\s*<summary>\s*"
    r"(?:System Info|A/B Experiments|Extensions[^<]*|Process Info|Workspace Info)"
    r"\s*</summary>.*?</details>",
    re.S,
)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.I)
_ATTACHMENT = re.compile(
    r"https://(?:github\.com/user-attachments|user-images\.githubusercontent\.com)/\S+"
)
_DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{60,}\b")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# A ticked template box and the plain bullets under it ("my steps:" lists).
_CHECKBOX = re.compile(r"^[ \t]*- \[[ xX]\].*\n(?:[ \t]*- (?!\[).*\n)*", re.M)
_HEADER = re.compile(r"^#{1,6}[ \t]+(?:\S+[ \t]*){1,5}$", re.M)  # "### Browser"
_NO_RESPONSE = re.compile(r"^_No response_$", re.M)
_BLANKS = re.compile(r"\n{3,}")


def _cap_fence(match: re.Match[str]) -> str:
    fence = match.group(0)
    if len(fence) <= FENCE_HEAD + FENCE_TAIL:
        return fence
    return f"{fence[:FENCE_HEAD]}\n[...]\n{fence[-FENCE_TAIL:]}"


def _clean_prose(text: str) -> str:
    text = _IMAGE.sub(r"\1", text)
    text = _HTML_IMAGE.sub("", text)
    text = _ATTACHMENT.sub("", text)
    text = _DATA_URI.sub("", text)
    text = _LONG_TOKEN.sub("", text)
    text = _CHECKBOX.sub("", text if text.endswith("\n") else text + "\n")
    text = _HEADER.sub("", text)
    return _NO_RESPONSE.sub("", text)


def clean_issue_text(body: str) -> str:
    """The body without what every report in the repo repeats or nobody reads.

    Template comments, the reporter's system blocks, ticked checkboxes, empty
    answers and section headers, screenshots, attachment links and long tokens
    go. Code fences stay but keep only their head and tail (a pasted log would
    otherwise fill the embedding). The rest of the cleaning skips fences, so a
    `# comment` in a shell block is never taken for a header.
    """
    body = _ANSI.sub("", body.replace("\r\n", "\n"))
    # Before the fence split: the reporter's blocks hold fences of their own.
    body = _REPORTER_DETAILS.sub("", _COMMENT.sub("", body))
    parts: list[str] = []
    end = 0
    for fence in _FENCE.finditer(body):
        parts.append(_clean_prose(body[end : fence.start()]))
        parts.append(_cap_fence(fence))
        end = fence.end()
    parts.append(_clean_prose(body[end:]))
    lines = "".join(parts).split("\n")
    return _BLANKS.sub("\n\n", "\n".join(line.rstrip() for line in lines)).strip()


def issue_input(issue: Issue) -> str:
    """Title and the cleaned body. Comments are left out on purpose: on a closed
    duplicate they name the answer ("duplicate of #N"), and a new issue has
    none yet anyway.
    """
    body = clean_issue_text(issue.body)[:MAX_BODY]
    return f"{issue.title}\n\n{body}" if body else issue.title


def create_schema(conn: Conn) -> None:
    conn.execute(SCHEMA)
    conn.commit()


def sync(conn: Conn, issues: Iterable[Issue], embed: Embed) -> IssueSyncReport:
    """Embed issues whose input text changed since the last sync.

    Keyed by a hash of the input, not `updated_at`: a new comment bumps
    `updated_at` but changes nothing that gets embedded.
    """
    create_schema(conn)
    todo: list[tuple[Issue, str, str]] = []
    unchanged = 0
    stored = {
        (r[0], r[1]): r[2]
        for r in conn.execute("SELECT repo, number, input_sha FROM issues")
    }
    for issue in issues:
        text = issue_input(issue)
        sha = hashlib.sha256(text.encode()).hexdigest()
        if stored.get((issue.repo, issue.number)) == sha:
            unchanged += 1
        else:
            todo.append((issue, text, sha))

    vectors = embed([text for _, text, _ in todo]) if todo else []
    if len(vectors) != len(todo):
        raise RuntimeError(f"asked for {len(todo)} embeddings, got {len(vectors)}")

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO issues (repo, number, created_at, input_sha, embedding)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (repo, number) DO UPDATE SET"
            " input_sha = EXCLUDED.input_sha, embedding = EXCLUDED.embedding",
            [
                (i.repo, i.number, i.created_at, sha, Vector(v))
                for (i, _, sha), v in zip(todo, vectors, strict=True)
            ],
        )
    return IssueSyncReport(embedded=len(todo), unchanged=unchanged)


def nearest(
    conn: Conn,
    repo: str,
    number: int,
    k: int,
    before: datetime,
    among: Sequence[int] | None = None,
) -> list[Neighbour]:
    """The k stored issues closest to issue `number`, created before `before`,
    optionally only among the issue numbers `among`.

    The time cut makes an eval honest: a duplicate can only point at an issue
    that existed when it was filed.
    """
    rows = conn.execute(
        "WITH q AS (SELECT embedding FROM issues"
        "           WHERE repo = %(repo)s AND number = %(n)s)"
        " SELECT i.number, 1 - (i.embedding <=> q.embedding) FROM issues i, q"
        " WHERE i.repo = %(repo)s AND i.number <> %(n)s"
        "   AND i.created_at < %(before)s"
        "   AND (%(among)s::int[] IS NULL OR i.number = ANY(%(among)s::int[]))"
        " ORDER BY i.embedding <=> q.embedding LIMIT %(k)s",
        {
            "repo": repo,
            "n": number,
            "before": before,
            "k": k,
            "among": list(among) if among is not None else None,
        },
    ).fetchall()
    return [Neighbour(number=r[0], score=float(r[1])) for r in rows]


def nearest_to(
    conn: Conn,
    repo: str,
    vector: list[float],
    k: int,
    among: Sequence[int] | None = None,
) -> list[Neighbour]:
    """The k stored issues closest to a vector (a plan, not an issue),
    optionally only among the issue numbers `among`."""
    rows = conn.execute(
        "SELECT number, 1 - (embedding <=> %(v)s) FROM issues"
        " WHERE repo = %(repo)s"
        "   AND (%(among)s::int[] IS NULL OR number = ANY(%(among)s::int[]))"
        " ORDER BY embedding <=> %(v)s LIMIT %(k)s",
        {
            "repo": repo,
            "v": Vector(vector),
            "k": k,
            "among": list(among) if among is not None else None,
        },
    ).fetchall()
    return [Neighbour(number=r[0], score=float(r[1])) for r in rows]


def embedding(conn: Conn, repo: str, number: int) -> list[float] | None:
    """The stored vector of one issue, None if it was never embedded."""
    row = conn.execute(
        "SELECT embedding FROM issues WHERE repo = %s AND number = %s",
        (repo, number),
    ).fetchone()
    return None if row is None else row[0].to_list()
