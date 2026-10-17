"""The removed features in the playground: the answer key for the feature
route (0.3).

`playground/features.toml` says per feature which files the reference
changes and which test files judge it; `features/remove/<key>.patch` takes
the feature out of the tip of main. See the header of features.toml for how
the set was made. The tickets live there too, not on GitHub: `ticket` turns
one into the `Issue` the loop takes.
"""

import re
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from cofabriq.issues.model import Issue
from cofabriq.loop.refine import git_env
from cofabriq.loop.run import CheckResult, Rules, repro_command
from cofabriq.loop.sandbox import Sandbox

_PATCHED = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


class Feature(BaseModel):
    key: str
    size: Literal["S", "M", "L"]
    files: list[str]  # source files the reference changes
    tests: list[str]  # test files at main that judge it (hidden)
    title: str
    body: str


_features_adapter = TypeAdapter(list[Feature])


def removal_path(features_file: Path, key: str) -> Path:
    return features_file.parent / "features" / "remove" / f"{key}.patch"


def patched_files(patch: str) -> set[str]:
    return set(_PATCHED.findall(patch))


def load_features(path: Path) -> list[Feature]:
    """The `[[feature]]` list, checked against the removal patches: every
    listed file and test file is one the patch changes."""
    features = _features_adapter.validate_python(
        tomllib.loads(path.read_text("utf-8"))["feature"]
    )
    keys = [f.key for f in features]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{path}: keys must be unique")
    for f in features:
        patch = removal_path(path, f.key)
        if not patch.is_file():
            raise ValueError(f"{f.key}: removal patch is missing")
        touched = patched_files(patch.read_text("utf-8"))
        unlisted = set(f.files + f.tests) - touched
        if unlisted:
            names = ", ".join(sorted(unlisted))
            raise ValueError(f"{f.key}: not in the patch: {names}")
        if not f.tests or not all(t.endswith(".test.ts") for t in f.tests):
            raise ValueError(f"{f.key}: tests must be vitest files (.test.ts)")
    return features


def ticket(feature: Feature, repo: str) -> Issue:
    """The feature's ticket as an open issue of `repo`, number 0 (not on
    GitHub). Title and body are treated as untrusted like any issue."""
    now = datetime.now(UTC)
    return Issue(
        repo=repo,
        number=0,
        title=feature.title,
        body=feature.body.strip(),
        state="open",
        state_reason=None,
        author=None,
        labels=[],
        created_at=now,
        updated_at=now,
        closed_at=None,
        comment_count=0,
        comments=[],
        duplicate_of=[],
        duplicates=[],
    )


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=True,
        env=git_env(),
    ).stdout


def remove(worktree: Path, patch: Path, key: str) -> None:
    """Applies the removal and commits it in the (detached) worktree, so
    every later diff against HEAD shows only the agent's work. The commit
    stays local: the worktree is never pushed."""
    _git(worktree, "apply", str(patch.resolve()))
    _git(worktree, "add", "-A")
    _git(
        worktree,
        "-c",
        "user.name=cofabriq",
        "-c",
        "user.email=cofabriq@localhost",
        "commit",
        "-q",
        "--no-verify",
        "-m",
        f"remove feature {key} (eval base)",
    )


def hidden_tests(
    worktree: Path, commit: str, feature: Feature, rules: Rules, sandbox: Sandbox
) -> CheckResult:
    """The reference test files from `commit` over the tree, run, then the
    tree's own versions put back: green = the feature is there."""
    originals = {t: (worktree / t).read_bytes() for t in feature.tests}
    try:
        for t in feature.tests:
            (worktree / t).write_text(_git(worktree, "show", f"{commit}:{t}"), "utf-8")
        ran = sandbox(repro_command(rules, feature.tests), worktree, network=False)
    finally:
        for t, data in originals.items():
            (worktree / t).write_bytes(data)
    return CheckResult(name="hidden", ok=ran.ok, seconds=ran.seconds, output=ran.output)


def _check(name: str, cmd: str, worktree: Path, sandbox: Sandbox) -> CheckResult:
    ran = sandbox(cmd, worktree, network=False)
    return CheckResult(
        name=name, ok=ran.ok, seconds=ran.seconds, output="" if ran.ok else ran.output
    )


class Proof(BaseModel):
    """One removal checked: the target's checks green without the feature,
    the hidden tests red without it (and green on `commit`, checked once)."""

    key: str
    checks: list[CheckResult]
    hidden: CheckResult

    @property
    def valid(self) -> bool:
        return all(c.ok for c in self.checks) and not self.hidden.ok


def prove(
    worktree: Path,
    commit: str,
    features: list[Feature],
    features_file: Path,
    rules: Rules,
    sandbox: Sandbox,
) -> list[Proof]:
    """Each removal in turn on one installed worktree at `commit` (reset in
    between; ignored files such as node_modules survive the reset)."""
    proofs: list[Proof] = []
    for f in features:
        _git(worktree, "reset", "-q", "--hard", commit)
        _git(worktree, "clean", "-q", "-fd")
        remove(worktree, removal_path(features_file, f.key), f.key)
        checks = [_check(c.name, c.cmd, worktree, sandbox) for c in rules.checks]
        hidden = hidden_tests(worktree, commit, f, rules, sandbox)
        proofs.append(Proof(key=f.key, checks=checks, hidden=hidden))
    _git(worktree, "reset", "-q", "--hard", commit)
    return proofs
