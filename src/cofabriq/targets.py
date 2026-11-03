"""Target repos: the per-repo config file and the names derived from a repo.

`targets/<owner>-<name>.toml` holds one table per concern (`[loop]`,
`[map]`, `[risk]`, ...). Each module that reads a table keeps its own model
and reads its table through `target_table`.
"""

import tomllib
from pathlib import Path
from typing import Any

PLAYGROUND = "philippmossier/local-ai-chat-playground"  # the demo target


def repo_slug(repo: str) -> str:
    """`owner/name` -> `owner-name`: the stem of every per-repo file and
    folder under `targets/`, `data/` and `evals/`. Not injective (`a-b/c`
    and `a/b-c` give the same slug); kept as is, stored paths depend on it."""
    return repo.replace("/", "-")


def target_table(targets_dir: Path, repo: str, name: str) -> dict[str, Any] | None:
    """Table `name` of the repo's target file; None if the file or the
    table is missing."""
    path = targets_dir / f"{repo_slug(repo)}.toml"
    if not path.is_file():
        return None
    return tomllib.loads(path.read_text("utf-8")).get(name)


def clone_dir(clones_dir: Path, repo: str) -> Path:
    return clones_dir / repo_slug(repo)
