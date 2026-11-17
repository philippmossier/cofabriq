"""Adding a repo from the browser (0.9): `repo add` and `repo check` as
background jobs the page starts and polls, one at a time per repo.

What a repo's setup is comes from the files `repo add` and `repo check`
write (`targets/own/<slug>.toml`, `.proposed.json`, `.check.json`), so the
CLI's repos show up here too and a restart loses nothing but the job that
was running; memory holds only what runs now and the last job's error.
"""

import threading
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from cofabriq.files import write_atomic
from cofabriq.loop.onboard import (
    MAX_REPAIRS,
    Checked,
    CheckStep,
    Citation,
    OnStep,
    Proposed,
    Repair,
    clone_source,
    config_hash,
    identity,
    load_proof,
    parse_target,
    proposal_path,
)
from cofabriq.loop.refine import CloneFrom, clone_from
from cofabriq.targets import OWN, own_target

type SetupPhase = Literal["proposing", "draft", "checking", "proven", "failed"]


class Refused(Exception):
    """A job that can't start; the message says why (the API answers 409)."""


class RepoSetup(BaseModel):
    """Where a repo's setup stands, for the page."""

    repo: str
    path: str  # the dev's clone ("" for a target without `[clone]`)
    phase: SetupPhase
    running: bool
    config: str  # the target file as saved; "" while the first proposal runs
    citations: list[Citation]  # each command's source, checked
    notes: str  # the agent's
    cost_usd: float  # the proposal's
    steps: list[CheckStep]  # the check's: live while it runs, else the last one
    error: str  # the last job failed before it could say more (agent, git)
    repairs_left: int  # proposals again with a failed check's output


# the dev's clone at `path` -> its proposal, saved (paid: one agent run);
# with a Repair: told how the last one failed its check
type Propose = Callable[[Path, Repair | None], Proposed]
# the repo's saved target file -> proven or not, saved (free, minutes)
type Prove = Callable[[str, OnStep], Checked]


class RepoJobs:
    def __init__(self, targets_dir: Path, propose: Propose, prove: Prove) -> None:
        self.targets_dir = targets_dir
        self.propose_fn = propose
        self.prove_fn = prove
        self._running: dict[str, Literal["propose", "check"]] = {}
        self._paths: dict[str, str] = {}  # a first proposal: no file says it yet
        self._steps: dict[str, list[CheckStep]] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def repos(self) -> list[str]:
        """Every own target, and a first proposal still running."""
        own = self.targets_dir / OWN
        stored = [p for p in own.glob("*.toml")] if own.is_dir() else []
        found = {r for p in stored if (r := self._repo_of(p)) is not None}
        with self._lock:
            found |= set(self._running)
        return sorted(found)

    def _repo_of(self, path: Path) -> str | None:
        """The repo a file is for: its proposal says, else its `[clone]`
        (a slug can't be read back: `a-b/c` and `a/b-c` share one)."""
        proposal = path.with_suffix(".proposed.json")
        if proposal.is_file():
            return Proposed.model_validate_json(proposal.read_text("utf-8")).repo
        table = tomllib.loads(path.read_text("utf-8")).get("clone")
        if table is None:
            return None
        try:  # a pydantic ValidationError is a ValueError too
            return identity(clone_source(CloneFrom.model_validate(table).path).path)
        except ValueError:
            return None

    def status(self, repo: str) -> RepoSetup | None:
        path = own_target(self.targets_dir, repo)
        with self._lock:
            running = self._running.get(repo)
            live = list(self._steps.get(repo, []))
            error = self._errors.get(repo, "")
            first = self._paths.get(repo, "")
        if running is None and not path.is_file():
            return None
        config = path.read_text("utf-8") if path.is_file() else ""
        proposal = proposal_path(self.targets_dir, repo)
        proposed = (
            Proposed.model_validate_json(proposal.read_text("utf-8"))
            if proposal.is_file()
            else None
        )
        source = clone_from(self.targets_dir, repo) if config else None
        proof = load_proof(self.targets_dir, repo) if config else None
        current = proof is not None and proof.config == config_hash(path)
        phase: SetupPhase
        if running == "propose":
            phase = "proposing"
        elif running == "check":
            phase = "checking"
        elif proof is not None and current:
            phase = "proven" if proof.proven else "failed"
        else:
            phase = "draft"
        # live while it runs; after it, the proof (a check that died: its steps)
        steps = proof.steps if running != "check" and proof and current else live
        return RepoSetup(
            repo=repo,
            path=str(source.path) if source is not None else first,
            phase=phase,
            running=running is not None,
            config=config,
            citations=proposed.citations if proposed is not None else [],
            notes=proposed.proposal.notes if proposed is not None else "",
            cost_usd=proposed.run.cost_usd if proposed is not None else 0.0,
            steps=steps,
            error=error,
            repairs_left=MAX_REPAIRS - (proposed.repairs if proposed else 0),
        )

    def propose(self, path: Path, again: bool = False, repair: bool = False) -> str:
        """Propose `[loop]` for the dev's clone at `path` in the background;
        the repo. `repair`: again, told how the last check failed (at most
        `MAX_REPAIRS` rounds). Refused: not a clone's root, a target there
        (without `again` or `repair`), nothing failed to repair, no rounds
        left, a job running for it."""
        try:
            source = clone_source(path)
        except ValueError as exc:
            raise Refused(str(exc)) from exc
        repo = identity(source.path)
        earlier = self._repair(repo) if repair else None
        if own_target(self.targets_dir, repo).is_file() and not (again or repair):
            raise Refused(f"{repo} is added already: open it")
        self._claim(repo, "propose")
        with self._lock:
            self._paths[repo] = str(source.path)
            self._steps.pop(repo, None)
        self._start(repo, lambda: self.propose_fn(source.path, earlier))
        return repo

    def _repair(self, repo: str) -> Repair:
        """The failed check to send back, as the page shows it."""
        found = self.status(repo)
        proof = load_proof(self.targets_dir, repo)
        if found is None or found.phase != "failed" or proof is None:
            raise Refused(f"{repo} has no failed check to propose again from")
        if found.repairs_left <= 0:
            raise Refused(f"proposed again {MAX_REPAIRS} times: edit the file instead")
        return Repair.of(found.config, proof, MAX_REPAIRS - found.repairs_left + 1)

    def check(self, repo: str, config: str) -> None:
        """Save the dev's edited target file (checked first), then prove it
        in the background. Refused: no such own target, a job running, or
        the file doesn't parse (the reason says where)."""
        path = own_target(self.targets_dir, repo)
        if not path.is_file():
            raise Refused(f"{repo} isn't added: add it first")
        try:
            parse_target(config, repo)
        except ValueError as exc:
            raise Refused(str(exc)) from exc
        self._claim(repo, "check")
        try:
            write_atomic(path, config.encode())
        except OSError:
            with self._lock:
                self._running.pop(repo, None)
            raise
        with self._lock:
            self._steps[repo] = []

        def step(s: CheckStep) -> None:
            with self._lock:
                steps = self._steps.setdefault(repo, [])
                if steps and steps[-1].name == s.name and steps[-1].ok is None:
                    steps[-1] = s  # the step that was running has ended
                else:
                    steps.append(s)

        self._start(repo, lambda: self.prove_fn(repo, step))

    def _claim(self, repo: str, job: Literal["propose", "check"]) -> None:
        with self._lock:
            if repo in self._running:
                raise Refused(f"{repo} is busy: wait for it")
            self._running[repo] = job
            self._errors.pop(repo, None)

    def _start(self, repo: str, work: Callable[[], object]) -> None:
        def run() -> None:
            try:
                work()
            except Exception as exc:  # shown on the page
                with self._lock:
                    self._errors[repo] = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._running.pop(repo, None)
                    self._paths.pop(repo, None)

        threading.Thread(target=run, daemon=True).start()
