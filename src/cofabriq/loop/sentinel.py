"""The sentinel (0.6): an invariant a target declares as its contract,
checked in code on every change, before any reviewer is paid for.

The playground's contract is "nothing typed leaves the device", and its
allow-list is written down: the hosts of its Content-Security-Policy. So
the check needs no model:

    a new URL in an added line    its host must be allowed by the policy
                                  (exact, or a `*.` wildcard); else a
                                  violation, the line is the evidence
    the policy file itself        any change goes to a human, always

Test files, docs and comment lines are not checked (they don't ship, or
don't run). A violation blocks: the run gets no PR. Evidence code can
check, like ADR-008's red test, not an agent's opinion.
"""

import re
import subprocess
import tomllib
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from cofabriq.loop.refine import git_env
from cofabriq.loop.run import is_test

_URL = re.compile(r"""https?://([A-Za-z0-9.-]+)""")
_COMMENT = re.compile(r"^\s*(//|\*|/\*|#|<!--)")


class Sentinel(BaseModel):
    """`[sentinel]` in a target file."""

    name: str
    policy_file: str  # where the allowed hosts are written (a CSP)
    about: str = ""  # the contract, in one line, for the dev


class Violation(BaseModel):
    kind: Literal["host", "policy"]
    path: str
    line: str  # the added line, the evidence
    host: str = ""


def load_sentinel(targets_dir: Path, repo: str) -> Sentinel | None:
    path = targets_dir / f"{repo.replace('/', '-')}.toml"
    if not path.is_file():
        return None
    table = tomllib.loads(path.read_text("utf-8")).get("sentinel")
    return None if table is None else Sentinel.model_validate(table)


def allowed_hosts(policy: str) -> set[str]:
    """The hosts a policy file names (`https://host` or `https://*.host`)."""
    return {
        m.group(0).split("://", 1)[1]
        for m in re.finditer(r"https://[*A-Za-z0-9.-]+", policy)
    }


def allowed(host: str, hosts: set[str]) -> bool:
    return any(host == h or (h.startswith("*.") and fnmatch(host, h)) for h in hosts)


def check(
    diff: str, hosts: set[str], sentinel: Sentinel, skip: list[str]
) -> list[Violation]:
    """The violations in a unified diff. `skip`: globs of files not checked
    (the target's test files); docs (`.md`) are never checked."""
    found: list[Violation] = []
    path = ""
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else ""
            continue
        if raw.startswith("--- ") or not path:
            continue
        changed = raw.startswith(("+", "-")) and not raw.startswith(("+++", "---"))
        if path == sentinel.policy_file and changed:
            found.append(Violation(kind="policy", path=path, line=raw))
            continue
        if not raw.startswith("+") or path.endswith(".md"):
            continue
        if is_test(path, skip) or _COMMENT.match(raw[1:]):
            continue
        for m in _URL.finditer(raw):
            if not allowed(m.group(1), hosts):
                found.append(
                    Violation(kind="host", path=path, line=raw, host=m.group(1))
                )
    return found


def explain(v: Violation, sentinel: Sentinel) -> str:
    line = v.line.strip()
    if v.kind == "policy":
        return f"{v.path}: the {sentinel.name} policy changed, a human decides: {line}"
    return f"{v.path}: {v.host} is not allowed by {sentinel.policy_file}: {line[1:]}"


def policy_at(clone: Path, commit: str, sentinel: Sentinel) -> str:
    """The policy file as of the run's commit: what the change may not
    widen (the agent's own edits to it don't count)."""
    out = subprocess.run(
        ["git", "-C", str(clone), "show", f"{commit}:{sentinel.policy_file}"],
        capture_output=True,
        text=True,
        check=True,
        env=git_env(),
    )
    return out.stdout


type Checker = Callable[[str], list[str]]


def checker(sentinel: Sentinel, policy: str, skip: list[str]) -> Checker:
    """diff -> the violations, explained; what the review gate calls."""
    hosts = allowed_hosts(policy)

    def run(diff: str) -> list[str]:
        return [explain(v, sentinel) for v in check(diff, hosts, sentinel, skip)]

    return run
