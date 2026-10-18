"""Intake: tickets in, the next one the loop may start out (0.3).

cofabriq does not split big features: the dev plans them in their own
session (interview, prototype, spec, tickets) and files the tickets on
GitHub, with blocking links between them. Intake reads those links and the
spec refine made, and says per ticket whether it may start:

    done     the ticket is closed as completed
    blocked  a blocker is still open (GitHub's native dependencies, or a
             `Blocked by #N` line in the body)
    waiting  a draft PR from cofabriq is still open: one open PR at a time,
             so the next slice builds on a merged one
    ready    may start

Readiness of a spec is checked in code, never by a model: criteria a test
can check (features), paths that exist, no open questions, size S or M.
Anything else goes back to the dev with the reason.
"""

import re
from typing import Literal

from pydantic import BaseModel

from cofabriq.issues.model import Issue, IssueRef
from cofabriq.loop.refine import Refined

type State = Literal["done", "blocked", "waiting", "ready"]

# "Blocked by #12", "blocked by: #12, #13", "Blocked by owner/name#7"
_BLOCKED_LINE = re.compile(r"(?im)^\W*blocked by:?\s+(.+)$")
_REF = re.compile(r"(?:([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+))?#(\d+)\b")


def blockers(issue: Issue) -> list[IssueRef]:
    """GitHub's native blockers, then those named in `Blocked by` lines of
    the body (same repo unless `owner/name#N`), without repeats."""
    found = list(issue.blocked_by)
    for line in _BLOCKED_LINE.findall(issue.body):
        for repo, number in _REF.findall(line):
            ref = IssueRef(repo=(repo or issue.repo).lower(), number=int(number))
            found.append(ref)
    return list(dict.fromkeys(r for r in found if r != _ref(issue)))


def _ref(issue: Issue) -> IssueRef:
    return IssueRef(repo=issue.repo, number=issue.number)


def done(issue: Issue) -> bool:
    return issue.state == "closed" and issue.state_reason in (None, "completed")


class Slice(BaseModel):
    number: int
    title: str
    state: State
    reason: str  # why it can't start; empty when ready or done


def slices(
    tickets: list[Issue], known: dict[IssueRef, Issue], open_prs: list[int]
) -> list[Slice]:
    """Each ticket's state, in the order given. `known`: the blockers'
    current issues (a blocker not in it counts as open: never guess done);
    `open_prs`: numbers of open draft PRs cofabriq made in this repo."""
    out: list[Slice] = []
    for t in tickets:
        if done(t):
            out.append(Slice(number=t.number, title=t.title, state="done", reason=""))
            continue
        open_blockers = [b for b in blockers(t) if b not in known or not done(known[b])]
        if open_blockers:
            names = ", ".join(_name(b, t.repo) for b in open_blockers)
            state, reason = "blocked", f"blocked by {names} (still open)"
        elif open_prs:
            prs = ", ".join(f"#{n}" for n in open_prs)
            state, reason = "waiting", f"cofabriq's PR {prs} is still open"
        else:
            state, reason = "ready", ""
        out.append(Slice(number=t.number, title=t.title, state=state, reason=reason))
    return out


def _name(ref: IssueRef, repo: str) -> str:
    return f"#{ref.number}" if ref.repo == repo else f"{ref.repo}#{ref.number}"


def not_ready(refined: Refined, tracked: set[str]) -> list[str]:
    """Why a spec can't go to the loop yet; empty = ready. `tracked`: the
    files at the spec's commit. Test files may be new, source files too
    when the spec says so (a feature adds files), so only a listed source
    file that is neither tracked nor said to be new counts."""
    s = refined.spec
    reasons: list[str] = []
    if refined.route == "feature" and not [c for c in s.criteria if c.strip()]:
        reasons.append("no acceptance criteria a test can check")
    missing = [
        f.path for f in s.files if f.path not in tracked and "new" not in f.why.lower()
    ]
    if missing:
        reasons.append(f"files not in the repo: {', '.join(missing)}")
    if s.open_questions:
        reasons.append(f"{len(s.open_questions)} open question(s) for the dev")
    if s.size == "L":
        reasons.append("size L: split it into tickets in your planning session")
    if not s.files:
        reasons.append("no source file named")
    return reasons
