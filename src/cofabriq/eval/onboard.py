"""`eval repo-add`: how often `repo add`'s proposal is proven by `repo
check` as the agent wrote it, before the dev edits anything.

The judge is the check itself (install, every check green, each suite's
canary red and green), not a model and not a reference file: a proposal
that differs from what a maintainer would write but proves the same thing
counts. What failed is kept per repo, so the misses can be read.
"""

import tomllib
from pathlib import Path

from pydantic import BaseModel

from cofabriq.loop import onboard
from cofabriq.loop.run import LoopRules


def load_set(path: Path) -> list[str]:
    """The repos of the set (`evals/repos.toml`), `owner/name` each."""
    repos: list[str] = tomllib.loads(path.read_text("utf-8"))["repos"]
    return repos


class Scored(BaseModel):
    repo: str
    commit: str
    repairs: int = 0  # 0: the first proposal; n: proposed again n times
    proven: bool
    failed: list[str]  # "install", a check's name, "canary <suite>"
    why: dict[str, str]  # the end of each failure's output, by its name
    cited: int
    unchecked: list[str]  # fields whose quote is not in their file
    cost_usd: float
    turns: int
    propose_s: float
    check_s: float
    rules: LoopRules
    notes: str


WHY = 600  # characters kept from the end of a failure's output


def failures(checked: onboard.Checked) -> dict[str, str]:
    """What failed, in order ("install", a check's name, "canary <suite>"),
    with the end of its output."""
    steps = [checked.install, *checked.checks]
    out = {c.name: c.output[-WHY:] for c in steps if not c.ok}
    for c in checked.canaries:
        if not (c.red and c.green):
            out[f"canary {c.suite}"] = c.output[-WHY:]
    return out


def score(proposed: onboard.Proposed, checked: onboard.Checked) -> Scored:
    why = failures(checked)
    return Scored(
        repo=proposed.repo,
        commit=proposed.commit,
        repairs=proposed.repairs,
        proven=checked.proven,
        failed=list(why),
        why=why,
        cited=len(proposed.citations),
        unchecked=[c.field for c in proposed.unchecked],
        cost_usd=proposed.run.cost_usd,
        turns=proposed.run.turns,
        propose_s=round(proposed.run.latency_ms / 1000, 1),
        check_s=checked.seconds,
        rules=proposed.proposal.rules(),
        notes=proposed.proposal.notes,
    )


class Summary(BaseModel):
    repos: int
    proven: int  # unedited, by the first proposal
    # repos proven after at most k proposals again: [first, after 1, after 2]
    proven_after: list[int]
    quotes_found: float  # share of cited values whose quote is in their file
    cost_usd: float  # per repo, mean, its proposals again included
    propose_s: float  # per proposal, mean


def summarize(scored: list[Scored]) -> Summary:
    """The first proposal's numbers as before (proven unedited, quotes),
    and how many more each round of proposing again proved."""
    first = [s for s in scored if s.repairs == 0]
    n = len(first) or 1
    cited = sum(s.cited for s in first) or 1
    rounds = max((s.repairs for s in scored), default=0)
    proven_after = [
        len({s.repo for s in scored if s.proven and s.repairs <= k})
        for k in range(rounds + 1)
    ]
    return Summary(
        repos=len(first),
        proven=sum(s.proven for s in first),
        proven_after=proven_after,
        quotes_found=round(1 - sum(len(s.unchecked) for s in first) / cited, 2),
        cost_usd=round(sum(s.cost_usd for s in scored) / n, 3),
        propose_s=round(sum(s.propose_s for s in scored) / (len(scored) or 1), 1),
    )
