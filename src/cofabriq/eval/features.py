"""Removed sets: the answer key for the feature route and for planted bugs
that live only in a patch.

`playground/features.toml` says per feature which files the reference
changes and which test files judge it; `features/remove/<key>.patch` takes
the feature out of the tip of main. See the header of features.toml for how
the set was made. The tickets live there too, not on GitHub: `ticket` turns
one into the `Issue` the loop takes. `evals/planted-cofabriq.toml` is a set
on cofabriq itself: there the set is in the repo under test, so `remove`
takes it out of the worktree with the plant.
"""

import re
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from cofabriq.eval.loop import Judged
from cofabriq.files import newest_by_key
from cofabriq.git import git
from cofabriq.issues.model import Issue
from cofabriq.loop.engine import Agent
from cofabriq.loop.refine import Refined, Route, refine
from cofabriq.loop.run import (
    CheckResult,
    LoopResult,
    LoopRules,
    is_test,
    repro_command,
)
from cofabriq.loop.sandbox import Sandbox
from cofabriq.loop.worktree import (
    add_worktree,
    commit_layer,
    keep_tests_at,
    remove_worktree,
    reset_hard,
)

_PATCHED = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


class Feature(BaseModel):
    """One entry of a removed set: a removed feature, or (`route =
    "bug"`) a planted hard bug, the same mechanism: a patch on main, main's
    test files as the hidden judge."""

    key: str
    route: Route = "feature"
    size: Literal["S", "M", "L"]
    files: list[str]  # source files the reference changes
    tests: list[str]  # test files at main that judge it (hidden)
    title: str
    body: str


_features_adapter = TypeAdapter(list[Feature])


def removal_path(features_file: Path, key: str) -> Path:
    """`playground/features.toml` -> `playground/features/remove/<key>.patch`
    (and `hard.toml` -> `hard/remove/`)."""
    return features_file.parent / features_file.stem / "remove" / f"{key}.patch"


def patched_files(patch: str) -> set[str]:
    return set(_PATCHED.findall(patch))


def load_features(path: Path, test_globs: list[str]) -> list[Feature]:
    """The `[[feature]]` list, checked against the removal patches: every
    listed file and test file is one the patch changes, and every test file
    one the target's `[loop] tests` globs match (a file the agent's own
    test could be)."""
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
        if not f.tests or not all(is_test(t, test_globs) for t in f.tests):
            raise ValueError(f"{f.key}: tests must be test files of the target")
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


def answer_key(features_file: Path) -> list[str]:
    """The set's own files (the toml and its folder of patches) as paths in
    the repo that holds them; none outside a repo."""
    try:
        top = git(features_file.resolve().parent, "rev-parse", "--show-toplevel")
    except subprocess.CalledProcessError:
        return []
    own = features_file.resolve().relative_to(Path(top.strip()).resolve())
    return [own.as_posix(), own.with_suffix("").as_posix()]


def remove(worktree: Path, features_file: Path, key: str) -> None:
    """Applies the removal and commits it in the (detached) worktree, so
    every later diff against HEAD shows only the agent's work. When the
    worktree is the repo that holds the set (cofabriq's own), the set goes
    too: the agent reads the worktree, and the set names the cause. The
    commit stays local (the worktree is never pushed); its message says
    nothing about the feature."""
    git(worktree, "apply", str(removal_path(features_file, key).resolve()))
    hide = [p for p in answer_key(features_file) if (worktree / p).exists()]
    if hide:
        git(worktree, "rm", "-r", "-q", "--", *hide)
    commit_layer(worktree, "eval base")


def hidden_tests(
    worktree: Path, commit: str, feature: Feature, rules: LoopRules, sandbox: Sandbox
) -> CheckResult:
    """The reference test files from `commit` over the tree, run, then the
    tree's own versions put back: green = the feature is there."""
    originals = {t: (worktree / t).read_bytes() for t in feature.tests}
    try:
        for t in feature.tests:
            (worktree / t).write_text(git(worktree, "show", f"{commit}:{t}"), "utf-8")
        ran = sandbox(repro_command(rules, feature.tests), worktree, network=False)
    finally:
        for t, data in originals.items():
            (worktree / t).write_bytes(data)
    return CheckResult.of("hidden", ran, keep_output=True)


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
    rules: LoopRules,
    sandbox: Sandbox,
) -> list[Proof]:
    """Each removal in turn on one installed worktree at `commit` (reset in
    between; ignored files such as node_modules survive the reset)."""
    proofs: list[Proof] = []
    for f in features:
        reset_hard(worktree, commit)
        remove(worktree, features_file, f.key)
        checks = [
            CheckResult.of(c.name, sandbox(c.cmd, worktree, network=False))
            for c in rules.checks
        ]
        hidden = hidden_tests(worktree, commit, f, rules, sandbox)
        proofs.append(Proof(key=f.key, checks=checks, hidden=hidden))
    reset_hard(worktree, commit)
    return proofs


def refine_feature(
    feature: Feature,
    features_file: Path,
    repo: str,
    clone: Path,
    commit: str,
    worktree: Path,
    agent: Agent,
    testing: str = "",
) -> Refined:
    """Refine on the feature's removed state: a worktree at `commit` with
    the removal committed, the ticket as the issue, the feature route. The
    spec is stored against `commit` (the removal commit is rebuilt by the
    loop, it lives only in the worktree)."""
    add_worktree(clone, worktree, commit)
    try:
        remove(worktree, features_file, feature.key)
        refined = refine(
            ticket(feature, repo), worktree, agent, feature.route, testing=testing
        )
    finally:
        remove_worktree(clone, worktree)
    return refined.model_copy(update={"commit": commit})


def removed_tests(features_file: Path, feature: Feature) -> list[str]:
    """The test lines the removal deletes: what the hidden tests check, for
    a dev to read next to the spec's criteria."""
    lines: list[str] = []
    current = ""
    patch = removal_path(features_file, feature.key).read_text("utf-8")
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("--- "):
            current = ""
        elif current in feature.tests and line.startswith("-") and line[1:].strip():
            lines.append(line[1:])
    return lines


def side_by_side(
    features_file: Path, features: list[Feature], specs: dict[str, Refined]
) -> str:
    """Markdown for the dev: per feature, the ticket's title, the spec's
    criteria and the hidden tests' removed lines."""
    parts = ["# Feature specs: criteria next to the hidden tests\n"]
    for f in features:
        parts.append(f"## {f.key} ({f.size}): {f.title}\n")
        spec = specs.get(f.key)
        if spec is None:
            parts.append("(no spec: the refine run failed)\n")
            continue
        parts.append("Criteria:\n")
        parts += [f"{n}. {c}" for n, c in enumerate(spec.spec.criteria, 1)]
        parts.append("\nRemoved test lines (the hidden tests):\n\n```ts")
        parts += removed_tests(features_file, f)
        parts.append("```\n")
    return "\n".join(parts)


def load_specs(refine_dir: Path) -> dict[str, Refined]:
    """Feature key -> its newest stored spec, over all `features-*.json`."""
    return newest_by_key(refine_dir, "features-*.json", TypeAdapter(Refined))


def on_reference(
    worktree: Path, commit: str, tests: list[str], rules: LoopRules, sandbox: Sandbox
) -> CheckResult:
    """The agent's acceptance test on the reference (main, the feature in):
    green = it tests the feature as it was built. Resets the worktree's
    code to `commit`, so it runs last."""
    keep_tests_at(worktree, tests, commit)
    ran = sandbox(repro_command(rules, tests), worktree, network=False)
    return CheckResult.of("valid", ran, keep_output=True)


def against_reference(
    result: LoopResult,
    worktree: Path,
    feature: Feature,
    rules: LoopRules,
    sandbox: Sandbox,
) -> Judged:
    """Judge (the hidden reference tests on the agent's code, when built)
    and validity (the agent's test on main). Needs the worktree the loop
    left; resets its code. `result.commit` is main."""
    judge, valid = None, None
    if result.status == "green":
        judge = hidden_tests(worktree, result.commit, feature, rules, sandbox)
    if result.repro:  # last: it resets the code
        valid = on_reference(worktree, result.commit, result.repro, rules, sandbox)
    return Judged(result=result, judge=judge, valid=valid)
