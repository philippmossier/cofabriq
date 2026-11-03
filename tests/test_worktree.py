from pathlib import Path

from cofabriq.loop.worktree import changed_paths, commit_layer, keep_tests_at
from tests.gitrepo import commit_all, git, init_repo


def test_changed_paths_lists_new_files_not_ignored(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a": "1", ".gitignore": "dist\n"})
    (repo / "a").write_text("2")
    (repo / "new").mkdir()
    (repo / "new" / "b").write_text("x")
    (repo / "dist").mkdir()
    (repo / "dist" / "c").write_text("x")
    assert changed_paths(repo) == ["a", "new/b"]


def test_keep_tests_at_puts_the_code_back_and_keeps_the_tests(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"src/a.ts": "base\n"})
    base = git(repo, "rev-parse", "HEAD")
    (repo / "src/a.ts").write_text("layer\n")
    commit_all(repo, "layer")
    (repo / "src/a.ts").write_text("fix\n")
    (repo / "src/b.ts").write_text("new\n")
    (repo / "src/a.test.sh").write_text("test\n")

    keep_tests_at(repo, ["src/a.test.sh"], base)

    assert git(repo, "rev-parse", "HEAD") == base
    assert (repo / "src/a.ts").read_text() == "base\n"
    assert not (repo / "src/b.ts").exists()
    assert (repo / "src/a.test.sh").read_text() == "test\n"


def test_commit_layer_skips_hooks_and_commits_an_empty_layer(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a": "1"})
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (repo / "a").write_text("2")

    commit_layer(repo, "the change")
    commit_layer(repo, "nothing")

    assert changed_paths(repo) == []
    log = git(repo, "log", "--format=%an %s", "-2")
    assert log.splitlines() == ["cofabriq nothing", "cofabriq the change"]
