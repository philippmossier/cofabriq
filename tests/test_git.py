from pathlib import Path

import pytest

from cofabriq.git import files_at, git, tracked, try_git
from tests.gitrepo import init_repo


def test_git_ignores_the_callers_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "r", {"a.txt": "1\n"})
    # as inside cofabriq's own pre-commit hook: an index of another repo
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "other-index"))
    assert tracked(repo) == ["a.txt"]
    assert files_at(repo, "HEAD") == ["a.txt"]


def test_try_git_answers_instead_of_raising(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a.txt": "1\n"})
    (repo / "a.txt").write_text("2\n")
    patch = tmp_path / "fix.patch"
    patch.write_text(git(repo, "diff"))
    git(repo, "checkout", "-q", "--", ".")

    assert try_git(repo, "apply", str(patch)) == (True, "")
    ok, err = try_git(repo, "apply", str(patch))  # already applied
    assert not ok and "patch does not apply" in err
