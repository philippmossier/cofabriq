from pathlib import Path

import pytest

from cofabriq.eval.features import (
    answer_key,
    load_features,
    patched_files,
    removal_path,
    remove,
    removed_tests,
    ticket,
)
from cofabriq.git import git  # without the caller's GIT_* (the hook runs this)
from cofabriq.loop.refine import Spec
from cofabriq.loop.run import render_spec
from cofabriq.loop.worktree import add_worktree
from tests.gitrepo import init_repo

ROOT = Path(__file__).parent.parent
PLAYGROUND = ROOT / "playground"
# the targets' `[loop] tests`: the playground's file, cofabriq's own (git-ignored)
PLAYGROUND_TESTS = ["src/**/*.test.ts", "e2e/**/*.spec.ts"]
COFABRIQ_TESTS = ["tests/test_*.py"]


def test_answer_key_matches_the_patches() -> None:
    features = load_features(PLAYGROUND / "features.toml", PLAYGROUND_TESTS)
    assert len(features) == 9
    for f in features:
        assert removal_path(PLAYGROUND / "features.toml", f.key).is_file()


def test_patched_files_reads_the_new_side() -> None:
    patch = "--- a/x.ts\n+++ b/x.ts\n@@ -1 +1 @@\n-a\n+b\n+++ not a header\n"
    assert patched_files(patch) == {"x.ts"}


def test_a_file_outside_the_patch_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "features" / "remove").mkdir(parents=True)
    (tmp_path / "features" / "remove" / "k.patch").write_text(
        "--- a/src/a.ts\n+++ b/src/a.ts\n--- a/src/a.test.ts\n+++ b/src/a.test.ts\n"
    )
    (tmp_path / "features.toml").write_text(
        '[[feature]]\nkey = "k"\nsize = "S"\nfiles = ["src/b.ts"]\n'
        'tests = ["src/a.test.ts"]\ntitle = "t"\nbody = "b"\n'
    )
    with pytest.raises(ValueError, match=r"not in the patch: src/b\.ts"):
        load_features(tmp_path / "features.toml", PLAYGROUND_TESTS)


def test_a_judge_must_be_a_test_file_of_the_target() -> None:
    with pytest.raises(ValueError, match="tests must be test files of the target"):
        load_features(PLAYGROUND / "hard.toml", COFABRIQ_TESTS)


def test_the_set_on_cofabriq_is_on_the_bug_route_with_pytest_judges() -> None:
    planted = load_features(ROOT / "evals" / "planted-cofabriq.toml", COFABRIQ_TESTS)
    assert len(planted) == 5 and {f.route for f in planted} == {"bug"}
    assert {f.size for f in planted} == {"S", "M"}


def test_the_answer_key_leaves_a_worktree_of_its_own_repo(tmp_path: Path) -> None:
    patch = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-right\n+planted\n"
    repo = init_repo(
        tmp_path / "repo",
        {
            "src/a.py": "right\n",
            "evals/set.toml": "[[feature]]\n",
            "evals/set/remove/k.patch": patch,
        },
    )
    assert answer_key(repo / "evals" / "set.toml") == ["evals/set.toml", "evals/set"]
    worktree = tmp_path / "w"
    add_worktree(repo, worktree, git(repo, "rev-parse", "HEAD").strip())
    remove(worktree, repo / "evals" / "set.toml", "k")
    assert (worktree / "src/a.py").read_text() == "planted\n"
    assert not (worktree / "evals").exists()
    assert git(worktree, "status", "--porcelain") == ""  # all in the base commit


def test_a_set_outside_the_repo_under_test_removes_only_the_plant(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo", {"src/a.py": "right\n", "evals/x.md": "x\n"})
    sets = tmp_path / "sets"
    (sets / "set" / "remove").mkdir(parents=True)
    (sets / "set.toml").write_text("[[feature]]\n")
    (sets / "set" / "remove" / "k.patch").write_text(
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-right\n+planted\n"
    )
    assert answer_key(sets / "set.toml") == []  # in no repo
    remove(repo, sets / "set.toml", "k")
    assert (repo / "src/a.py").read_text() == "planted\n"
    assert (repo / "evals/x.md").exists()
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "log", "-1", "--format=%s").strip() == "eval base"


def test_ticket_is_an_open_issue_without_a_number() -> None:
    f = load_features(PLAYGROUND / "features.toml", PLAYGROUND_TESTS)[0]
    issue = ticket(f, "owner/repo")
    assert (issue.number, issue.state, issue.title) == (0, "open", f.title)
    assert issue.body == f.body.strip()


def test_removed_tests_are_the_deleted_test_lines() -> None:
    features = {
        f.key: f for f in load_features(PLAYGROUND / "features.toml", PLAYGROUND_TESTS)
    }
    lines = removed_tests(PLAYGROUND / "features.toml", features["german-anywhere"])
    assert lines == [
        '    [["en-US", "de"], "de"], // any preferred German wins: '
        "a German reader can read the German text"
    ]


def test_render_spec_shows_criteria_to_the_agent() -> None:
    spec = Spec(
        problem="p",
        files=[],
        tests=[],
        criteria=["detectLang(['en', 'de']) is 'de'"],
        plan=[],
        assumptions=[],
        open_questions=[],
        size="S",
    )
    shown = render_spec(spec)
    assert "acceptance criteria:\n- detectLang(['en', 'de']) is 'de'" in shown


def test_on_reference_runs_the_agents_test_on_main(tmp_path: Path) -> None:
    from cofabriq.eval.features import on_reference
    from cofabriq.loop.worktree import add_worktree
    from tests.builders import AGENT_RULES
    from tests.fakes import HostSandbox
    from tests.gitrepo import git, init_repo

    clone = init_repo(tmp_path / "clone", {"src/a.ts": "fixed\n"})
    commit = git(clone, "rev-parse", "HEAD")
    worktree = tmp_path / "w"
    add_worktree(clone, worktree, commit)
    (worktree / "src/a.ts").write_text("removed\n")  # the agent's build, gone
    (worktree / "src/a.test.sh").write_text('grep -q fixed "$(dirname "$0")/a.ts"\n')
    ran = on_reference(worktree, commit, ["src/a.test.sh"], AGENT_RULES, HostSandbox())
    assert ran.ok
    assert (worktree / "src/a.ts").read_text() == "fixed\n"


def test_the_hard_set_is_on_the_bug_route() -> None:
    hard = load_features(PLAYGROUND / "hard.toml", PLAYGROUND_TESTS)
    assert len(hard) == 5 and {f.route for f in hard} == {"bug"}
    assert removal_path(PLAYGROUND / "hard.toml", hard[0].key).parent.name == "remove"
    assert {f.size for f in hard} == {"S", "M"}
