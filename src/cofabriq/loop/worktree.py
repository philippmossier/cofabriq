"""The throwaway worktrees the loop and the evals work in: add and remove,
what changed, put files back, layers committed locally.

A worktree is detached and never pushed; its commits exist only so that a
later diff measures one step's change and not the ones before it.
"""

import shutil
import threading
from pathlib import Path

from cofabriq.git import git, tracked

# git keeps worktree bookkeeping in the clone's .git: parallel runs (eval)
# add and remove their worktrees one at a time
_worktrees = threading.Lock()

_COFABRIQ = ["-c", "user.name=cofabriq", "-c", "user.email=cofabriq@localhost"]


def add_worktree(clone: Path, path: Path, commit: str) -> None:
    """A fresh detached worktree of `clone` at `commit` (an old one at the
    same path is removed first)."""
    remove_worktree(clone, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _worktrees:
        git(clone, "worktree", "add", "-q", "--detach", str(path.resolve()), commit)


def remove_worktree(clone: Path, path: Path) -> None:
    with _worktrees:
        if path.exists():
            git(clone, "worktree", "remove", "--force", str(path.resolve()))
        git(clone, "worktree", "prune")


def changed_paths(worktree: Path) -> list[str]:
    """Tracked files changed and untracked files added (ignored ones, like
    node_modules, excluded)."""
    out = git(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
    paths: list[str] = []
    entries = iter(out.split("\0"))
    for entry in entries:
        if not entry:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":  # renames carry the old path as the next entry
            paths.append(next(entries, ""))
    return sorted(p for p in dict.fromkeys(paths) if p)


def restore(worktree: Path, paths: list[str], originals: dict[str, bytes]) -> None:
    """Put protected paths back: the copied repro from its bytes, tracked
    files from HEAD, anything else (a new file) removed."""
    in_index = set(tracked(worktree))
    for p in paths:
        target = worktree / p
        if p in originals:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(originals[p])
        elif p in in_index:
            git(worktree, "checkout", "HEAD", "--", p)
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def diff(worktree: Path, exclude: list[str], base: str = "HEAD") -> str:
    """The change against `base`, new files included, `exclude` left out."""
    return _staged(worktree, base, exclude)


def changed_since(worktree: Path, base: str, exclude: list[str]) -> list[str]:
    """Paths changed against `base` (commits in the worktree included)."""
    names = _staged(worktree, base, exclude, "--name-only")
    return sorted(p for p in names.splitlines() if p)


def _staged(worktree: Path, base: str, exclude: list[str], *flags: str) -> str:
    git(worktree, "add", "-A")
    try:
        skip = [f":(exclude){p}" for p in exclude]
        return git(worktree, "diff", "--cached", *flags, base, "--", ".", *skip)
    finally:
        git(worktree, "reset", "-q")  # the index stays as the agent left it


def reset(worktree: Path) -> None:
    """Back to HEAD: tracked files restored, untracked ones removed (ignored
    ones, like node_modules, kept)."""
    git(worktree, "checkout", "-q", "HEAD", "--", ".")
    git(worktree, "clean", "-fdq")


def reset_hard(worktree: Path, commit: str) -> None:
    """Back to `commit`, layers on top dropped: tracked files restored,
    untracked ones removed (ignored ones kept)."""
    git(worktree, "reset", "-q", "--hard", commit)
    git(worktree, "clean", "-q", "-fd")


def keep_tests_at(worktree: Path, tests: list[str], base: str | None = None) -> None:
    """The code back to `base` (default HEAD) with `tests` as they are now:
    a test written against one tree, run on another."""
    kept = {p: (worktree / p).read_bytes() for p in tests}
    if base is not None:
        git(worktree, "checkout", "-qf", "--detach", base)
    reset(worktree)
    for p, data in kept.items():
        (worktree / p).parent.mkdir(parents=True, exist_ok=True)
        (worktree / p).write_bytes(data)


def commit_layer(worktree: Path, message: str) -> None:
    """Everything in the worktree as a local commit by cofabriq, so the next
    step's diff is measured against it. The clone's hooks don't run; an
    empty layer is still a commit."""
    git(worktree, "add", "-A")
    git(
        worktree,
        *_COFABRIQ,
        "commit",
        "-q",
        "--no-verify",
        "--allow-empty",
        "-m",
        message,
    )


def place_planted(worktree: Path, dest: str, repro: Path) -> None:
    """A planted repro (the answer key's test) copied to `dest` in the
    worktree."""
    (worktree / dest).parent.mkdir(parents=True, exist_ok=True)
    (worktree / dest).write_bytes(repro.read_bytes())
