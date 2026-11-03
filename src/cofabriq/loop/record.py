"""The tail of a run's graph (`loop/graph.py`, which `cofabriq run` and
the flow both drive): the target's sentinel for the review gate, the draft
PR, and the run record in data/runs (`<stem>.json`, `<stem>.pr.md` next to
it) that the viewer and `lessons` read."""

import json
from pathlib import Path

from pydantic import BaseModel

from cofabriq.loop import pr as pr_mod
from cofabriq.loop import sentinel as sentinel_mod
from cofabriq.loop.gate import Gated, Round
from cofabriq.loop.refine import Route, Spec
from cofabriq.loop.review import Review
from cofabriq.loop.run import CheckResult, LoopResult
from cofabriq.map import components as components_mod


class RunRecord(BaseModel):
    """One run as stored, by the graph's `record` step: the loop's result
    before the review gate (the gate's final one is in `gate.result`), the
    judge's verdicts only on a planted bug. Keys the writer never set stay
    out of the file. Older flow records hold the gate's result here."""

    result: LoopResult
    judge: CheckResult | None = None  # the planted repro on the final tree
    valid: CheckResult | None = None  # the agent's repro with the reference fix
    evidence: list[str] = []  # screenshot names in data/runs/<stem>/
    gate: Gated | None = None  # None: no review ran (red loop, halted)
    draft: pr_mod.Draft | None = None  # None: blocked, or no review
    published: dict[str, object] | None = None  # the PR, or {"error": ...}


def sentinel_for(
    targets_dir: Path, repo: str, clone: Path, commit: str, tests: list[str]
) -> sentinel_mod.Checker | None:
    """The target's invariant check, its policy read at the run's commit."""
    found = sentinel_mod.load_sentinel(targets_dir, repo)
    if found is None:
        return None
    policy = sentinel_mod.policy_at(clone, commit, found)
    return sentinel_mod.checker(found, policy, tests)


def draft_for(
    targets_dir: Path,
    repo: str,
    *,
    title: str,
    spec: Spec,
    result: LoopResult,
    tests: list[str],
    review: Review | None,
    rounds: list[Round] | None = None,
    route: Route = "bug",
    evidence: int = 0,
) -> pr_mod.Draft:
    """The draft PR for a change, rated by the target's risk rules and
    components."""
    return pr_mod.draft(
        issue_title=title,
        spec=spec,
        result=result,
        tests=tests,
        risk_rules=pr_mod.load_risk(targets_dir, repo),
        components=components_mod.load_rules(targets_dir, repo),
        review=review,
        rounds=rounds,
        route=route,
        evidence=evidence,
    )


def write_record(runs_dir: Path, stem: str, record: RunRecord) -> Path:
    """`<stem>.json`, plus the PR body as `<stem>.pr.md` when there is a
    draft. Top-level keys as the writer set them (a flow has no `judge`),
    the values in full."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    if record.draft is not None:
        (runs_dir / f"{stem}.pr.md").write_text(record.draft.body, "utf-8")
    dumped = record.model_dump(mode="json")
    keys = [k for k in dumped if k in record.model_fields_set]
    out = runs_dir / f"{stem}.json"
    out.write_text(json.dumps({k: dumped[k] for k in keys}, indent=2), "utf-8")
    return out
