"""The planted bugs in the playground: the answer key for refine and the loop.

`playground/bugs.toml` says per bug which files the fix changes and which
test reproduces it; the issue a user would file is the seed entry with the
same key. See the header of bugs.toml for how the bugs were planted.
"""

import subprocess
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from cofabriq.eval.triage import load_seed
from cofabriq.loop.refine import git_env
from cofabriq.loop.run import Rules, git, repro_command
from cofabriq.loop.sandbox import Sandbox


class PlantedBug(BaseModel):
    key: str
    kind: Literal["logic", "ui"]
    size: Literal["S", "M", "L"]
    files: list[str]
    repro: str  # file name in bugs/repro/


_bugs_adapter = TypeAdapter(list[PlantedBug])


def load_bugs(path: Path, seed: Path) -> list[PlantedBug]:
    """The `[[bug]]` list, checked against the seed issues and the repro
    and fix files next to it."""
    bugs = _bugs_adapter.validate_python(tomllib.loads(path.read_text("utf-8"))["bug"])
    keys = [b.key for b in bugs]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{path}: keys must be unique")
    issues = {s.key: s for s in load_seed(seed)}
    folder = path.parent / "bugs"
    for b in bugs:
        if b.key not in issues or issues[b.key].type != "bug":
            raise ValueError(f"{b.key}: no seed issue of type bug with this key")
        if not (folder / "repro" / b.repro).is_file():
            raise ValueError(f"{b.key}: repro {b.repro} is missing")
        if not (folder / "fixes" / f"{b.key}.patch").is_file():
            raise ValueError(f"{b.key}: fix patch is missing")
        suffix = ".test.ts" if b.kind == "logic" else ".spec.ts"
        if not b.repro.endswith(suffix):
            raise ValueError(f"{b.key}: a {b.kind} repro ends in {suffix}")
    return bugs


def planted_for(title: str, bugs: list[PlantedBug], seed: Path) -> PlantedBug | None:
    """The planted bug whose seed issue has this title (seed keys carry no
    GitHub numbers, see `triage.match`)."""
    keys = {s.title: s.key for s in load_seed(seed)}
    return next((b for b in bugs if b.key == keys.get(title)), None)


def repro_path(bugs_file: Path, bug: PlantedBug) -> Path:
    return bugs_file.parent / "bugs" / "repro" / bug.repro


def fix_path(bugs_file: Path, bug: PlantedBug) -> Path:
    return bugs_file.parent / "bugs" / "fixes" / f"{bug.key}.patch"


class BugProof(BaseModel):
    """One planted bug checked: its repro red on the planted code, green
    with only its own reference fix."""

    key: str
    applies: bool
    red: bool
    green: bool

    @property
    def valid(self) -> bool:
        return self.applies and self.red and self.green


def prove_bugs(
    worktree: Path,
    commit: str,
    bugs: list[PlantedBug],
    bugs_file: Path,
    rules: Rules,
    sandbox: Sandbox,
) -> list[BugProof]:
    """Each bug in turn on one installed worktree at `commit` (reset in
    between; ignored files such as node_modules survive the reset)."""
    proofs: list[BugProof] = []
    for b in bugs:
        git(worktree, "reset", "-q", "--hard", commit)
        git(worktree, "clean", "-q", "-fd")
        dest = rules.planted_path(b.repro)
        (worktree / dest).parent.mkdir(parents=True, exist_ok=True)
        (worktree / dest).write_bytes(repro_path(bugs_file, b).read_bytes())
        cmd = repro_command(rules, [dest])
        red = not sandbox(cmd, worktree, network=False).ok
        applied = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "apply",
                str(fix_path(bugs_file, b).resolve()),
            ],
            capture_output=True,
            check=False,
            env=git_env(),
        )
        applies = applied.returncode == 0
        green = applies and sandbox(cmd, worktree, network=False).ok
        proofs.append(BugProof(key=b.key, applies=applies, red=red, green=green))
    git(worktree, "reset", "-q", "--hard", commit)
    git(worktree, "clean", "-q", "-fd")
    return proofs
