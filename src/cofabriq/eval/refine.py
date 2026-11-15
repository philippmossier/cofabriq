"""Refine against the planted bugs: are the files in the spec the files the
reference fix changes, and is the size estimate right? The removed
features are scored the same way.

Truth is `files` in `playground/bugs.toml` or `features.toml` (code only, so the spec's
`tests` don't count). The spec's `files` are ranked, most likely first:
hit@k = at least one truth file in the top k, recall@k = share of truth
files in the top k, precision = share of all listed files that are truth.
Rates are reported per bug size too: the planted bugs are mostly small,
and one overall rate would hide that.

Lines (`score_lines`, bugs with a reference fix): does the spec's `at`
name a line the fix changes, does a sketch it gives (kept: its old lines
are in the file) cover one, and does it plan a test at all.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from cofabriq.loop.refine import Spec

KS = (1, 3)


@dataclass(frozen=True)
class BugScore:
    key: str
    size: str
    hit: dict[int, bool]
    recall: dict[int, float]
    precision: float
    listed: int  # files in the spec
    size_guess: str


class Answer(Protocol):
    """An answer key entry: a planted bug or a removed feature."""

    @property
    def key(self) -> str: ...
    @property
    def size(self) -> str: ...
    @property
    def files(self) -> list[str]: ...


def score_bug(bug: Answer, spec: Spec) -> BugScore:
    """Docs the spec lists (`.md`) are left out like the truth leaves them
    out: a feature's spec rightly names the doc it updates."""
    code = [f.path for f in spec.files if not f.path.endswith(".md")]
    listed = list(dict.fromkeys(code))
    truth = set(bug.files)

    def found(k: int) -> int:
        return len(truth & set(listed[:k]))

    return BugScore(
        key=bug.key,
        size=bug.size,
        hit={k: found(k) > 0 for k in KS},
        recall={k: found(k) / len(truth) for k in KS},
        precision=len(truth & set(listed)) / len(listed) if listed else 0.0,
        listed=len(listed),
        size_guess=spec.size,
    )


def fixed_lines(patch: str) -> dict[str, set[int]]:
    """Per file, the lines of the original a unified diff changes: the lines
    it removes, and both neighbours of a line it only inserts (one added
    right after removed ones replaces them: those count already)."""
    out: dict[str, set[int]] = {}
    path, n, replacing = "", 0, False
    for line in patch.splitlines():
        if line.startswith("--- a/"):
            path = line.removeprefix("--- a/")
        elif m := re.match(r"^@@ -(\d+)", line):
            n, replacing = int(m[1]), False
        elif line.startswith("-") and not line.startswith("---"):
            out.setdefault(path, set()).add(n)
            n, replacing = n + 1, True
        elif line.startswith("+") and not line.startswith("+++"):
            if not replacing:
                out.setdefault(path, set()).update({n - 1, n})
        elif line.startswith(" "):
            n, replacing = n + 1, False
    return out


@dataclass(frozen=True)
class LineScore:
    """Where a spec says the change goes, against the reference fix: per bug,
    over the files the fix changes."""

    key: str
    at: bool | None  # `at` names a line the fix changes; None: no `at` given
    edits: int  # sketches kept (checked against the file)
    edit_hit: bool | None  # a kept sketch covers a line the fix changes
    tests: bool  # the spec plans a test at all


def score_lines(key: str, spec: Spec, fixed: dict[str, set[int]]) -> LineScore:
    def covered(path: str, ranges: list[tuple[int, int]]) -> bool:
        return any(fixed[path] & set(range(a, b + 1)) for a, b in ranges)

    files = [f for f in spec.files if f.path in fixed]
    with_at = [f for f in files if f.at]
    sketched = [f for f in files if f.edits]

    def at_ranges(at: list[str]) -> list[tuple[int, int]]:
        return [(int(a.split("-")[0]), int(a.split("-")[-1])) for a in at]

    return LineScore(
        key=key,
        at=any(covered(f.path, at_ranges(f.at)) for f in with_at) if with_at else None,
        edits=sum(len(f.edits) for f in spec.files),
        edit_hit=any(
            covered(f.path, [(e.line, e.line + e.old.count("\n")) for e in f.edits])
            for f in sketched
        )
        if sketched
        else None,
        tests=bool(spec.tests),
    )


@dataclass
class Summary:
    n: int = 0
    hit: dict[int, float] = field(default_factory=dict[int, float])
    recall: dict[int, float] = field(default_factory=dict[int, float])
    precision: float = 0.0
    size_ok: float = 0.0
    listed: float = 0.0


def summarize(scores: Iterable[BugScore]) -> Summary:
    items = list(scores)
    n = len(items)
    if n == 0:
        return Summary()
    return Summary(
        n=n,
        hit={k: sum(s.hit[k] for s in items) / n for k in KS},
        recall={k: sum(s.recall[k] for s in items) / n for k in KS},
        precision=sum(s.precision for s in items) / n,
        size_ok=sum(s.size == s.size_guess for s in items) / n,
        listed=sum(s.listed for s in items) / n,
    )
