"""The result handed back (0.9): a reviewed change as a branch in
cofabriq's own clone plus a patch file, for the dev to pull into their own
folder and push themselves. Nothing here writes to the dev's folder or to
GitHub (`publish.py` is the one place that writes to GitHub).

    the run's final diff, on top of the spec's commit
      -> commit 1: the test files (the test the dev approved, red on the base)
      -> commit 2: the rest (the change), message: the PR title and the
         spec's problem (plain text: the PR body is GitHub markdown with
         the diff in it, which `git am` would take for the patch)
      -> branch `cofabriq/<n>-<slug>` in the clone (a new run replaces it)
      -> both as `git format-patch` into data/runs/<stem>.patch

The commits are built from the stored diff in a throwaway index: the
clone's checkout, its index and the run's worktree (with its layer
commits) stay as they are; git only gains objects and one branch. Author
and committer are cofabriq: it wrote the change.
"""

import re
import shlex
import tempfile
import textwrap
from pathlib import Path

from pydantic import BaseModel

from cofabriq.git import AS_COFABRIQ, COFABRIQ_EMAIL, git, git_env

BRANCH_PREFIX = "cofabriq/"  # every branch cofabriq makes or pushes


class Handoff(BaseModel):
    """What a run left to take, or why it left nothing."""

    branch: str = ""
    base: str = ""  # the spec's commit: the branch starts there
    commits: int = 0  # 2: the test, then the change; 1: one of them is empty
    patch: str = ""  # its file name in data/runs
    error: str = ""  # git failed: no branch, no patch


def slug(title: str) -> str:
    """An issue title (untrusted) reduced to [a-z0-9-], at most 40 long."""
    cut = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")
    return cut or "fix"


def branch_for(key: str, title: str) -> str:
    """`cofabriq/51-chat-title-is-cut-off-too-early`: one per ticket (a
    local one: `cofabriq/L3-...`)."""
    return f"{BRANCH_PREFIX}{key}-{slug(title)}"


def take_command(clone: str, branch: str) -> str:
    """The shell line the dev runs in their own folder to take `branch`
    from cofabriq's `clone`. A rerun of the ticket rebuilt the branch from
    the base, so the dev's branch of that name can't fast-forward to it:
    the line fetches into FETCH_HEAD and moves the branch there (`switch
    -C`), but only while every commit on it that the new one lacks is
    cofabriq's. A commit of the dev's own (or one they amended) stops it
    with a message; their branch stays as it is."""
    b, src = shlex.quote(branch), shlex.quote(clone)
    theirs = (
        f"git log --format=%ae refs/heads/{b} --not FETCH_HEAD -- 2>/dev/null"
        f" | grep -vx {shlex.quote(COFABRIQ_EMAIL)}"
    )
    why = f"{branch} has commits of yours: rename it, then run this again"
    refuse = f"echo {shlex.quote(why)} >&2"
    return (
        f'git fetch {src} {b} && {{ test -z "$({theirs})" || {{ {refuse}; false; }}; }}'
        f" && git switch -C {b} FETCH_HEAD"
    )


def tree_of(clone: Path, base: str, diff: str, only: list[str] | None = None) -> str:
    """The tree of `base` with `diff` applied, built in a throwaway index;
    `only`: just these files of the diff."""
    with tempfile.TemporaryDirectory() as tmp:
        env = git_env() | {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(clone, "read-tree", base, env=env)
        if only != []:
            pick = [f"--include={_literal(p)}" for p in only or []]
            git(clone, "apply", "--cached", *pick, "-", input=diff, env=env)
        return git(clone, "write-tree", env=env).strip()


def _message_body(text: str) -> str:
    """Long lines wrapped at 72 columns (git's convention), line breaks kept.
    `git am` takes the first line of a commit message that looks like a
    diff (`---`, `diff -`, `Index: `) for the patch's start: such a line
    gets a leading space, so the patch still applies."""
    lines = [textwrap.fill(line, 72) for line in text.strip().splitlines()]
    return re.sub(r"(?m)^(?=---|diff -|Index: )", " ", "\n".join(lines))


def _literal(path: str) -> str:
    """`git apply --include` takes a glob: the path's own glob characters
    escaped, so it matches only itself."""
    return re.sub(r"([*?\[\\])", r"\\\1", path)


def hand_off(
    clone: Path,
    *,
    base: str,
    diff: str,
    tests: list[str],
    title: str,
    body: str,
    branch: str,
    patch: Path,
) -> Handoff:
    """`diff` on `base` as two commits on `branch` (the files in `tests`
    first) and as a patch at `patch`. An empty part makes no commit."""
    if not diff.strip():
        return Handoff(base=base, error="nothing to hand off: the diff is empty")
    head = base
    parts = [
        (tests, f"{title}: the test, red before the change"),
        (None, f"{title}\n\n{_message_body(body)}"),
    ]
    made = 0
    for only, message in parts:
        tree = tree_of(clone, base, diff, only)
        if tree == git(clone, "rev-parse", f"{head}^{{tree}}").strip():
            continue
        made += 1
        head = git(
            clone, *AS_COFABRIQ, "commit-tree", tree, "-p", head, "-m", message
        ).strip()
    if made == 0:
        return Handoff(base=base, error="nothing to hand off: the diff changes nothing")
    git(clone, "branch", "-f", branch, head)  # a new run on the issue replaces it
    patch.parent.mkdir(parents=True, exist_ok=True)
    patches = git(
        clone, "format-patch", "--stdout", "--no-signature", f"{base}..{head}"
    )
    patch.write_text(patches, "utf-8")
    return Handoff(branch=branch, base=base, commits=made, patch=patch.name)
