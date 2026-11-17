"""Target repos: the per-repo config file and the names derived from a repo.

`targets/<owner>-<name>.toml` holds one table per concern (`[loop]`,
`[map]`, `[risk]`, ...). Each module that reads a table keeps its own model
and reads its table through `target_table`.

`targets/own/` (git-ignored) holds the dev's own repos, written by
`cofabriq repo add`; a file there wins over a shipped one of the same name.
An own target is used by the loop only once `repo check` proved it
(`loop/onboard.py`); a shipped one is reviewed in git and proven by its evals.
"""

import tomllib
from pathlib import Path
from typing import Any

PLAYGROUND = "philippmossier/local-ai-chat-playground"  # the demo target
OWN = "own"  # the folder under the targets dir for the dev's own repos


def repo_slug(repo: str) -> str:
    """`owner/name` -> `owner-name`: the stem of every per-repo file and
    folder under `targets/`, `data/` and `evals/`. Not injective (`a-b/c`
    and `a/b-c` give the same slug); kept as is, stored paths depend on it."""
    return repo.replace("/", "-")


def own_target(targets_dir: Path, repo: str) -> Path:
    """Where `repo add` writes the dev's own target file."""
    return targets_dir / OWN / f"{repo_slug(repo)}.toml"


def target_file(targets_dir: Path, repo: str) -> Path | None:
    """The repo's target file: the dev's own first, then a shipped one."""
    own = own_target(targets_dir, repo)
    shipped = targets_dir / f"{repo_slug(repo)}.toml"
    return next((p for p in (own, shipped) if p.is_file()), None)


def target_table(targets_dir: Path, repo: str, name: str) -> dict[str, Any] | None:
    """Table `name` of the repo's target file; None if the file or the
    table is missing."""
    path = target_file(targets_dir, repo)
    if path is None:
        return None
    return tomllib.loads(path.read_text("utf-8")).get(name)


def clone_dir(clones_dir: Path, repo: str) -> Path:
    return clones_dir / repo_slug(repo)
