"""Refine against the planted bugs: are the files in the spec the files the
reference fix changes, and is the size estimate right? The removed
features (0.3) are scored the same way.

Truth is `files` in `playground/bugs.toml` or `features.toml` (code only, so the spec's
`tests` don't count). The spec's `files` are ranked, most likely first:
hit@k = at least one truth file in the top k, recall@k = share of truth
files in the top k, precision = share of all listed files that are truth.
Rates are reported per bug size too: the planted bugs are mostly small,
and one overall rate would hide that.
"""

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
