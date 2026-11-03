"""git on a target repo, a clone or a worktree: every call goes through here."""

import os
import subprocess
from pathlib import Path


def git_env() -> dict[str, str]:
    """The environment for git on a target repo: without the caller's
    `GIT_*` variables. Inside a git hook (cofabriq's own pre-commit runs the
    tests) `GIT_INDEX_FILE` and friends would point git at the wrong repo."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _run(
    cwd: Path, args: tuple[str, ...], input: str | None, env: dict[str, str] | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=input,
        capture_output=True,
        encoding="utf-8",
        check=False,
        env=git_env() if env is None else env,
    )


def git(
    cwd: Path, *args: str, input: str | None = None, env: dict[str, str] | None = None
) -> str:
    """git's stdout; `CalledProcessError` (stderr attached) when it fails.
    `env` replaces `git_env()`: build it on top of that."""
    done = _run(cwd, args, input, env)
    done.check_returncode()
    return done.stdout


def try_git(cwd: Path, *args: str, input: str | None = None) -> tuple[bool, str]:
    """For a command whose failure is an answer (`git apply`): ok, stderr."""
    done = _run(cwd, args, input, None)
    return done.returncode == 0, done.stderr


def files_at(cwd: Path, rev: str) -> list[str]:
    """Every file path in the tree of `rev`."""
    return git(cwd, "ls-tree", "-r", "--name-only", rev).splitlines()


def tracked(worktree: Path) -> list[str]:
    """Every path in the worktree's index."""
    return [p for p in git(worktree, "ls-files", "-z").split("\0") if p]
