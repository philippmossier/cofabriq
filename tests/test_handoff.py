"""The result handed back: a branch in cofabriq's clone and a patch, built
from the stored diff; the dev fetches the branch or applies the patch in
their own folder."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from cofabriq.loop.handoff import Handoff, branch_for, hand_off, take_command
from cofabriq.loop.record import RunRecord, handoff_for, write_record
from cofabriq.serve import build
from cofabriq.targets import clone_dir
from tests.builders import loop_result
from tests.gitrepo import git, init_repo

DIFF = """\
diff --git a/src/a.ts b/src/a.ts
--- a/src/a.ts
+++ b/src/a.ts
@@ -1 +1 @@
-bug
+fixed
diff --git a/src/a.test.sh b/src/a.test.sh
new file mode 100644
--- /dev/null
+++ b/src/a.test.sh
@@ -0,0 +1 @@
+grep -q fixed
"""

BRANCH = "cofabriq/51-title-cut"


def setup(tmp_path: Path) -> tuple[Path, str]:
    clone = init_repo(tmp_path / "clone", {"src/a.ts": "bug\n"})
    return clone, git(clone, "rev-parse", "HEAD")


def take(
    clone: Path,
    base: str,
    diff: str = DIFF,
    tests: list[str] | None = None,
    body: str = "the body",
) -> Handoff:
    return hand_off(
        clone,
        base=base,
        diff=diff,
        tests=["src/a.test.sh"] if tests is None else tests,
        title="Fix #51: title cut",
        body=body,
        branch=BRANCH,
        patch=clone.parent / "runs" / "r1.patch",
    )


def test_the_test_first_then_the_change_by_cofabriq(tmp_path: Path) -> None:
    clone, base = setup(tmp_path)
    got = take(clone, base)
    assert got == Handoff(branch=BRANCH, base=base, commits=2, patch="r1.patch")
    log = git(clone, "log", "--format=%an <%ae> %cn|%s", f"{base}..{BRANCH}")
    by = "cofabriq <cofabriq@localhost> cofabriq"
    assert log.splitlines() == [
        f"{by}|Fix #51: title cut",
        f"{by}|Fix #51: title cut: the test, red before the change",
    ]
    # the test commit has only the test; the change's message has the body
    assert (
        git(clone, "show", "--name-only", "--format=", f"{BRANCH}~1") == "src/a.test.sh"
    )
    assert git(clone, "log", "-1", "--format=%b", BRANCH) == "the body"
    # the clone's checkout and index are as they were
    assert git(clone, "rev-parse", "HEAD") == base
    assert git(clone, "status", "--porcelain") == ""


def test_the_dev_fetches_the_branch_or_applies_the_patch(tmp_path: Path) -> None:
    clone, base = setup(tmp_path)
    # a message line that looks like a diff would be taken for the patch
    take(clone, base, body="the cause:\ndiff --git a/x b/x\n--- a/x\nIndex: x")
    mine = tmp_path / "mine"  # the dev's own folder, at the same base
    git(tmp_path, "clone", "-q", str(clone), str(mine))
    git(mine, "fetch", "-q", str(clone), f"{BRANCH}:{BRANCH}")
    git(mine, "switch", "-q", BRANCH)
    assert (mine / "src/a.ts").read_text() == "fixed\n"

    git(mine, "switch", "-q", "--detach", base)
    patch = (tmp_path / "runs" / "r1.patch").read_text()
    git_am = ["-c", "user.name=t", "-c", "user.email=t@t", "am", "-q"]
    git(mine, *git_am, str(tmp_path / "runs" / "r1.patch"))
    assert (mine / "src/a.test.sh").read_text() == "grep -q fixed\n"
    assert (mine / "src/a.ts").read_text() == "fixed\n"
    assert patch.count("\nSubject: ") == 2
    body = git(mine, "log", "-1", "--format=%b")
    assert body == "the cause:\n diff --git a/x b/x\n --- a/x\n Index: x"


def test_a_new_run_replaces_the_branch(tmp_path: Path) -> None:
    clone, base = setup(tmp_path)
    take(clone, base)
    first = git(clone, "rev-parse", BRANCH)
    again = DIFF.replace("+fixed", "+fixed again")
    take(clone, base, again)
    assert git(clone, "rev-parse", BRANCH) != first
    assert git(clone, "show", f"{BRANCH}:src/a.ts") == "fixed again"


def test_an_empty_part_makes_no_commit(tmp_path: Path) -> None:
    clone, base = setup(tmp_path)
    assert take(clone, base, tests=[]).commits == 1  # a planted repro: no test
    assert git(clone, "rev-list", "--count", f"{base}..{BRANCH}") == "1"
    empty = take(clone, base, diff="")
    assert empty.error and not empty.branch and not empty.patch


def test_a_test_path_with_glob_characters_matches_only_itself(tmp_path: Path) -> None:
    clone = init_repo(tmp_path / "clone", {"app/[id]/page.ts": "bug\n"})
    base = git(clone, "rev-parse", "HEAD")
    diff = DIFF.replace("src/a.ts", "app/[id]/page.ts").replace(
        "src/a.test.sh", "app/[id]/page.test.sh"
    )
    take(clone, base, diff, tests=["app/[id]/page.test.sh"])
    test_commit = git(clone, "show", "--name-only", "--format=", f"{BRANCH}~1")
    assert test_commit == "app/[id]/page.test.sh"


def test_handoff_for_keeps_gits_failure_instead_of_raising(tmp_path: Path) -> None:
    clone, base = setup(tmp_path)
    result = loop_result("green", 1, ["src/a.ts"]).model_copy(
        update={"commit": base, "diff": DIFF.replace("-bug", "-not there")}
    )
    got = handoff_for(
        clone,
        tmp_path / "runs" / "r1.patch",
        key="51",
        issue_title="t",
        result=result,
        title="Fix #51: t",
        problem="p",
        tests=["src/**/*.test.sh"],
    )
    assert got.error.startswith("git: ") and got.branch == ""
    assert not (tmp_path / "runs" / "r1.patch").exists()


def test_the_viewer_gives_the_fetch_command_and_only_the_records_patch(
    tmp_path: Path,
) -> None:
    runs, clones = tmp_path / "runs", tmp_path / "my clones"  # a space to quote
    result = loop_result("green", 1, ["src/a.ts"])
    stem = "acme-app-51-20261117T101500"
    handoff = Handoff(branch=BRANCH, base="b", commits=2, patch=f"{stem}.patch")
    write_record(runs, stem, RunRecord(result=result, handoff=handoff))
    (runs / f"{stem}.patch").write_text("From abc\n")
    (runs / "other.patch").write_text("not this one")
    c = TestClient(build(tmp_path, runs, clones, None))

    view = c.get(f"/api/runs/{stem}/{result.number}").json()["handoff"]
    clone = str(clone_dir(clones, result.repo).resolve())
    assert view["clone"] == clone
    assert view["fetch"] == take_command(clone, BRANCH)
    assert view["fetch"].startswith(f"git fetch '{clone}' {BRANCH} && ")
    assert view["fetch"].endswith(f" && git switch -C {BRANCH} FETCH_HEAD")
    got = c.get(view["patch_url"])
    assert got.text == "From abc\n"
    assert got.headers["content-type"].startswith("text/x-diff")
    assert got.headers["content-disposition"].startswith("attachment")

    # a record naming a path, or no patch at all: nothing is served
    bad = handoff.model_copy(update={"patch": "../secret.patch"})
    write_record(
        runs, "acme-app-52-20261117T101500", RunRecord(result=result, handoff=bad)
    )
    write_record(runs, "acme-app-53-20261117T101500", RunRecord(result=result))
    for name in ["acme-app-52-20261117T101500", "acme-app-53-20261117T101500", "other"]:
        assert c.get(f"/api/patches/{name}").status_code == 404, name


AGAIN = DIFF.replace("+fixed", "+fixed again")


def card_clone(tmp_path: Path) -> tuple[Path, str]:
    """cofabriq's clone where the viewer looks for it, a space to quote."""
    where = clone_dir(tmp_path / "my clones", "o/r")
    clone = init_repo(where, {"src/a.ts": "bug\n"})
    return clone, git(clone, "rev-parse", "HEAD")


def card_fetch(tmp_path: Path, handoff: Handoff) -> str:
    """The command the viewer's "Take it" card gives for this handoff."""
    runs, clones = tmp_path / "runs", tmp_path / "my clones"
    result = loop_result("green", 1, ["src/a.ts"])
    stem = "acme-app-51-20261117T101500"
    write_record(runs, stem, RunRecord(result=result, handoff=handoff))
    c = TestClient(build(tmp_path, runs, clones, None))
    return c.get(f"/api/runs/{stem}/{result.number}").json()["handoff"]["fetch"]


def run_card(mine: Path, fetch: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", fetch], cwd=mine, capture_output=True, text=True, check=False
    )


def dev_on_the_branch(tmp_path: Path) -> tuple[Path, Path, str]:
    clone, base = card_clone(tmp_path)
    fetch = card_fetch(tmp_path, take(clone, base))
    mine = tmp_path / "mine"
    git(tmp_path, "clone", "-q", str(clone), str(mine))
    ran = run_card(mine, fetch)
    assert ran.returncode == 0, ran.stderr
    return clone, mine, base


def test_the_fetch_command_works_the_first_time(tmp_path: Path) -> None:
    _, mine, _ = dev_on_the_branch(tmp_path)
    assert git(mine, "branch", "--show-current") == BRANCH
    assert (mine / "src/a.ts").read_text() == "fixed\n"


def test_the_fetch_command_takes_a_rerun_while_the_branch_is_checked_out(
    tmp_path: Path,
) -> None:
    clone, mine, base = dev_on_the_branch(tmp_path)
    fetch = card_fetch(tmp_path, take(clone, base, AGAIN))
    ran = run_card(mine, fetch)
    assert ran.returncode == 0, ran.stderr
    assert git(mine, "branch", "--show-current") == BRANCH
    assert (mine / "src/a.ts").read_text() == "fixed again\n"


def test_the_fetch_command_takes_a_rerun_from_another_branch(tmp_path: Path) -> None:
    clone, mine, base = dev_on_the_branch(tmp_path)
    git(mine, "switch", "-q", "--detach", base)
    fetch = card_fetch(tmp_path, take(clone, base, AGAIN))
    ran = run_card(mine, fetch)
    assert ran.returncode == 0, ran.stderr
    assert git(mine, "rev-parse", BRANCH) == git(clone, "rev-parse", BRANCH)
    assert (mine / "src/a.ts").read_text() == "fixed again\n"


def test_the_fetch_command_never_drops_a_commit_of_the_devs(tmp_path: Path) -> None:
    clone, mine, base = dev_on_the_branch(tmp_path)
    (mine / "NOTES").write_text("mine\n")
    git(mine, "add", "NOTES")
    # the whole identity: CI's git has no user.name to fall back on
    dev = ("-c", "user.name=dev", "-c", "user.email=dev@example.com")
    git(mine, *dev, "commit", "-qm", "my own")
    kept = git(mine, "rev-parse", "HEAD")

    ran = run_card(mine, card_fetch(tmp_path, take(clone, base, AGAIN)))
    assert ran.returncode != 0
    assert f"{BRANCH} has commits of yours" in ran.stderr
    assert git(mine, "rev-parse", BRANCH) == kept


def test_the_branch_is_one_per_issue_and_safe() -> None:
    assert branch_for("51", "../../x; rm -rf ~") == "cofabriq/51-x-rm-rf"
    assert branch_for("3", "!!!") == "cofabriq/3-fix"
    assert branch_for("L3", "a local one") == "cofabriq/L3-a-local-one"
