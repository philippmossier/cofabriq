"""A repo of the dev's own: its `[loop]` proposed by a read-only agent, then
proven in the sandbox before the loop may use it (`cofabriq repo add`,
`repo check`).

The proposal cites, for every command, the repo file it came from and the
line in it (a package.json script, a Makefile target, a CI step). A quote
that is not in that file is flagged for the dev, never trusted; the dev
reads and edits the file before anything runs.

The check runs on the base commit: the install (the only network), every
check green, and per suite two canary tests, one that must fail and one
that must pass. The loop rests on a red test the dev freezes; a suite
command that passes no matter what (a glob that misses the test folder,
`--passWithNoTests`) would make that test worthless, and only a red canary
shows it. The proof is stored next to the target file with that file's
hash, so an edit after the check takes it back.
"""

import hashlib
import re
import shlex
import time
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, ConfigDict

from cofabriq.files import stamp
from cofabriq.git import git, try_git
from cofabriq.issues.model import check_repo
from cofabriq.loop.engine import Agent, AgentRun
from cofabriq.loop.refine import CloneFrom
from cofabriq.loop.run import Check, CheckResult, LoopRules, Suite, install
from cofabriq.loop.sandbox import Sandbox
from cofabriq.loop.worktree import add_worktree, remove_worktree, reset_hard
from cofabriq.targets import OWN, own_target, repo_slug, target_file
from cofabriq.untrusted import quote_in

INSTRUCTIONS = """\
You set up a repository you have never seen for an automated coding loop.
Read its manifests and lockfiles, Makefile or task runner, CI workflows,
CONTRIBUTING and README to find how a developer installs it and checks a
change. Change nothing.

The loop runs every command as a shell line in a fresh Linux container
from image, the repository mounted at /app as the working directory. Only
install has network: it runs once, first. Everything after it runs
without network, so the checks and tests can use only what install put in
the repository folder or in the caches that persist between containers
(XDG_CACHE_HOME, GOMODCACHE, npm's cache); the home folder and anything
installed globally in the image do not. The container gets the files but
not the git history: no git commands.

- image: a public container image with the toolchain at the version the
  repo pins (.nvmrc, .python-version, go.mod, engines, CI). A tag, never
  latest. With browser tests: Microsoft's Playwright image of the locked
  Playwright version.
- setup: a shell line run first in every container, also those without
  network (corepack enable, where the image's Node still has corepack), or
  empty.
- install: one shell line installing from the lockfile as it is (frozen,
  no updates). It must not change tracked files. A tool that fetches more
  on its first run (a type checker downloading its runtime, browsers) is
  warmed here, or it fails later without network.
- checks: what CI runs on a pull request that a code change must keep
  green: types, lint, tests. name: short (types, lint, test). cmd: the
  repo's own scripts where they exist. Leave out formatters that rewrite
  files, deploys, and anything needing secrets, services (a database) or
  network; say so in notes.
- suites: the kinds of tests the repo has (unit, browser, ...), at least
  one. A test writer adds one test file, which runs as cmd with the file's
  path appended. suffix: the file name ending that picks the suite
  (".test.ts", "_test.go", ".py"); dir: an existing test folder where a new
  test of this kind belongs; about: one line telling the test writer what
  this kind tests. canary_name: a file name in dir ending with suffix that
  the runner collects; canary_fail: the whole content of that file, one
  test asserting that 1 equals 2; canary_pass: the same asserting 1
  equals 1.
- tests: glob patterns matching every test file of the suites: the only
  files a test writer may create or change.
- repro_checks: names of checks a new test must pass on its own (types,
  lint); never the check that runs the tests.
- protected: files a fix must never change: manifests, lockfiles, test,
  lint and type checker config.
- source and quote, for image, install, every check and every suite: the
  repo file the command comes from (package.json, Makefile,
  .github/workflows/ci.yml) and the line in it, copied exactly. Empty when
  no file says it (an image no file pins).
- notes: what you left out and why, what you could not find.
"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cited(_Strict):
    """A command (or the image) and where in the repo it comes from."""

    value: str
    source: str  # a repo path; empty: no file says it
    quote: str  # the line in `source`, copied exactly


class ProposedCheck(_Strict):
    name: str
    cmd: Cited


class ProposedSuite(_Strict):
    name: str
    suffix: str
    cmd: Cited
    dir: str
    about: str
    canary_name: str
    canary_fail: str
    canary_pass: str


class Proposal(_Strict):
    """What the agent answers: `[loop]` with a source for every command."""

    image: Cited
    setup: str
    install: Cited
    checks: list[ProposedCheck]
    suites: list[ProposedSuite]
    tests: list[str]
    repro_checks: list[str]
    protected: list[str]
    notes: str

    def cited(self) -> list[tuple[str, Cited]]:
        """Every cited value with the field it is in."""
        return [
            ("image", self.image),
            ("install", self.install),
            *[(f"check {c.name}", c.cmd) for c in self.checks],
            *[(f"suite {s.name}", s.cmd) for s in self.suites],
        ]

    def rules(self) -> LoopRules:
        """As `[loop]`. A path no suite claims runs with the first suite's
        command (`repro`); planted repros are an eval's, not the dev's."""
        suites = [
            Suite(
                name=s.name,
                suffix=s.suffix,
                cmd=s.cmd.value,
                dir=s.dir,
                about=s.about,
                canary_name=s.canary_name,
                canary_fail=s.canary_fail,
                canary_pass=s.canary_pass,
            )
            for s in self.suites
        ]
        first = suites[0] if suites else None
        return LoopRules(
            image=self.image.value,
            setup=self.setup,
            install=self.install.value,
            repro_dir=first.dir if first else "",
            repro=first.cmd if first else "",
            checks=[Check(name=c.name, cmd=c.cmd.value) for c in self.checks],
            protected=self.protected,
            tests=self.tests,
            repro_checks=self.repro_checks,
            suites=suites,
        )


class Citation(BaseModel):
    """One cited value, checked against the file at the commit."""

    field: str
    value: str
    source: str
    quote: str
    found: bool  # the quote is in that file


class Proposed(BaseModel):
    repo: str
    commit: str
    proposal: Proposal
    citations: list[Citation]
    run: AgentRun

    @property
    def unchecked(self) -> list[Citation]:
        return [c for c in self.citations if not c.found]


def identity(path: Path) -> str:
    """`owner/name` from the clone's `origin` remote (GitHub or any host:
    its last two path segments); `local/<folder>` without one."""
    ok, _ = try_git(path, "remote", "get-url", "origin")
    url = git(path, "remote", "get-url", "origin").strip() if ok else ""
    found = re.search(r"[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    if found:
        return check_repo(f"{found[1]}/{found[2]}")
    folder = re.sub(r"[^\w.-]", "-", path.resolve().name)
    return check_repo(f"local/{folder}")


def base_branch(path: Path) -> str:
    """The branch runs start from: the one `origin/HEAD` points at, else
    the branch checked out now."""
    ok, _ = try_git(path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if ok:
        ref = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        return ref.strip().removeprefix("origin/")
    branch = git(path, "branch", "--show-current").strip()
    if not branch:
        raise ValueError(f"{path}: no branch checked out and no origin/HEAD")
    return branch


def agent_schema() -> dict[str, Any]:
    return Proposal.model_json_schema()


def propose(repo: str, clone: Path, agent: Agent) -> Proposed:
    """One read-only agent run in the clone, its citations checked against
    the files at the clone's commit."""
    commit = git(clone, "rev-parse", "HEAD").strip()
    run = agent(INSTRUCTIONS, agent_schema(), clone)
    proposal = Proposal.model_validate(run.output)
    citations = [
        Citation(
            field=field,
            value=c.value,
            source=c.source,
            quote=c.quote,
            found=_quoted(clone, commit, c),
        )
        for field, c in proposal.cited()
    ]
    return Proposed(
        repo=repo, commit=commit, proposal=proposal, citations=citations, run=run
    )


def _quoted(clone: Path, commit: str, c: Cited) -> bool:
    if not c.source.strip() or not c.quote.strip():
        return False
    ok, _ = try_git(clone, "cat-file", "-e", f"{commit}:{c.source.strip()}")
    if not ok:
        return False
    return quote_in(c.quote, git(clone, "show", f"{commit}:{c.source.strip()}"))


def _comment(text: str, width: int = 100) -> str:
    """Untrusted text as one comment line: no line break can end the
    comment and put a key into the file."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 3] + "..."


def render(proposed: Proposed, source: CloneFrom | None) -> str:
    """The target file `repo add` writes: what was checked as comments on
    top (lost if anything rewrites the file), then `[clone]` (none: from
    GitHub, as the eval clones) and `[loop]`."""
    run = proposed.run
    lines = [
        f"# Proposed by `cofabriq repo add` for {proposed.repo} at"
        f" {proposed.commit[:7]} ({run.model}, ${run.cost_usd:.2f}).",
        "# Read it and edit what is wrong, then prove it in the sandbox:",
        f"#   uv run cofabriq repo check {proposed.repo}",
        "#",
        "# Where each command comes from (checked: the quote is in that file):",
    ]
    for c in proposed.citations:
        mark = "ok     " if c.found else "UNCHECKED"
        where = f"{c.source}: {c.quote}" if c.source else "no file says it"
        lines.append(f"#   {mark} {c.field}: {_comment(where)}")
    if proposed.proposal.notes.strip():
        lines += ["#", f"# Agent's notes: {_comment(proposed.proposal.notes, 400)}"]
    loop = proposed.proposal.rules().model_dump(
        exclude_none=True, exclude_defaults=True
    )
    data: dict[str, Any] = {"loop": loop}
    if source is not None:
        data = {"clone": {"path": str(source.path), "branch": source.branch}} | data
    return "\n".join(lines) + "\n\n" + tomli_w.dumps(data, multiline_strings=True)


class Canary(BaseModel):
    suite: str
    red: bool  # the failing canary failed
    green: bool  # the passing canary passed
    output: str  # the run that went the wrong way, if one did


class Checked(BaseModel):
    """`repo check`'s proof, stored as `<slug>.check.json` next to the own
    target files; `config` is the hash of the target file it proved."""

    repo: str
    commit: str
    config: str
    at: str
    install: CheckResult
    checks: list[CheckResult]
    canaries: list[Canary]
    seconds: float

    @property
    def proven(self) -> bool:
        return (
            self.install.ok
            and bool(self.checks)
            and all(c.ok for c in self.checks)
            and bool(self.canaries)
            and all(c.red and c.green for c in self.canaries)
        )


def check(
    repo: str,
    rules: LoopRules,
    *,
    config: str,
    clone: Path,
    commit: str,
    worktree: Path,
    sandbox: Sandbox,
) -> Checked:
    """Install, every check, and each suite's canaries, on a fresh
    worktree at `commit` (removed afterwards). A failed install stops it.
    `config`: the hash of the target file `rules` came from."""
    start = time.perf_counter()
    add_worktree(clone, worktree, commit)
    checks: list[CheckResult] = []
    canaries: list[Canary] = []
    try:
        installed = install(rules, worktree, sandbox, planted=[])
        if installed.ok:
            checks = [
                CheckResult.of(c.name, sandbox(c.cmd, worktree, network=False))
                for c in rules.checks
            ]
            canaries = [_canary(s, worktree, commit, sandbox) for s in rules.suites]
    finally:
        remove_worktree(clone, worktree)
    return Checked(
        repo=repo,
        commit=commit,
        config=config,
        at=stamp(),
        install=installed,
        checks=checks,
        canaries=canaries,
        seconds=round(time.perf_counter() - start, 1),
    )


def _canary(suite: Suite, worktree: Path, commit: str, sandbox: Sandbox) -> Canary:
    name, fail, ok = suite.canary_name, suite.canary_fail, suite.canary_pass
    if not (name and fail and ok) or not name.endswith(suite.suffix):
        why = f"no canary ending in {suite.suffix!r} in [[loop.suites]] {suite.name}"
        return Canary(suite=suite.name, red=False, green=False, output=why)
    rel = f"{suite.dir.rstrip('/')}/{name}"
    path = worktree / rel
    cmd = f"{suite.cmd} {shlex.quote(rel)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fail, "utf-8")
    failed = sandbox(cmd, worktree, network=False)
    path.write_text(ok, "utf-8")
    passed = sandbox(cmd, worktree, network=False)
    reset_hard(worktree, commit)
    path.unlink(missing_ok=True)  # in an ignored folder, the reset keeps it
    wrong = failed.output if failed.ok else passed.output if not passed.ok else ""
    return Canary(suite=suite.name, red=not failed.ok, green=passed.ok, output=wrong)


def config_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def proof_path(targets_dir: Path, repo: str) -> Path:
    return targets_dir / OWN / f"{repo_slug(repo)}.check.json"


def unproven(targets_dir: Path, repo: str) -> str | None:
    """Why the loop may not use the repo's target yet; None: it may. A
    shipped target needs no proof; an own one a green check of the file
    as it is now."""
    path = target_file(targets_dir, repo)
    if path is None:
        return f"no target file for {repo}"
    if path != own_target(targets_dir, repo):
        return None
    proof = proof_path(targets_dir, repo)
    run = f"uv run cofabriq repo check {repo}"
    if not proof.is_file():
        return f"not checked yet: {run}"
    checked = Checked.model_validate_json(proof.read_text("utf-8"))
    if checked.config != config_hash(path):
        return f"the target file changed since its check: {run}"
    if not checked.proven:
        return f"its last check failed: {run}"
    return None
