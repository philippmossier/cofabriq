"""Adding a repo from the browser: the propose and check jobs, what the
page sees while they run and after, and what is refused."""

import threading
import time
from pathlib import Path

import pytest
import tomli_w
from fastapi.testclient import TestClient

from cofabriq.loop import onboard
from cofabriq.loop.graph import FlowRunner
from cofabriq.loop.repo_jobs import Refused, RepoJobs, RepoSetup
from cofabriq.loop.run import CheckResult
from cofabriq.serve import build
from cofabriq.targets import own_target
from tests.gitrepo import git
from tests.test_onboard import REPO, Proposer, app_repo, proposal, rules


class Jobs:
    """RepoJobs with fake work: the proposal from a fake agent, the check
    reporting two steps; each waits for `go` so a test sees it running."""

    def __init__(self, targets: Path, proven: bool = True) -> None:
        self.targets = targets
        self.proven = proven
        self.go = threading.Event()
        self.earlier: list[onboard.Repair | None] = []
        self.jobs = RepoJobs(targets, self.propose, self.prove)

    def propose(self, path: Path, earlier: onboard.Repair | None) -> onboard.Proposed:
        self.go.wait(5)
        self.earlier.append(earlier)
        source = onboard.clone_source(path)
        proposed = onboard.propose(REPO, path, Proposer(proposal()), earlier)
        onboard.save_proposal(self.targets, proposed, source)
        return proposed

    def prove(self, repo: str, on_step: onboard.OnStep) -> onboard.Checked:
        on_step(onboard.CheckStep(name="install", ok=None))
        self.go.wait(5)
        on_step(onboard.CheckStep(name="install", ok=True))
        ok = CheckResult(name="install", ok=True, seconds=1, output="")
        test = CheckResult(name="test", ok=self.proven, seconds=1, output="1 failed")
        checked = onboard.Checked(
            repo=repo,
            commit="c",
            config=onboard.config_hash(own_target(self.targets, repo)),
            at="now",
            install=ok,
            checks=[test],
            canaries=[onboard.Canary(suite="sh", red=True, green=True, output="")],
            seconds=2,
        )
        onboard.save_proof(self.targets, checked)
        return checked


def settled(jobs: RepoJobs, repo: str = REPO) -> RepoSetup:
    for _ in range(200):
        found = jobs.status(repo)
        if found is not None and not found.running:
            return found
        time.sleep(0.01)
    raise AssertionError("the job never ended")


def mine(tmp_path: Path) -> Path:
    clone = app_repo(tmp_path)
    git(clone, "branch", "-M", "main")
    git(clone, "remote", "add", "origin", "https://github.com/acme/app.git")
    return clone


def test_a_proposal_runs_in_the_background_then_waits_for_the_dev(
    tmp_path: Path,
) -> None:
    f = Jobs(tmp_path / "targets")
    assert f.jobs.propose(mine(tmp_path)) == REPO

    running = f.jobs.status(REPO)
    assert running is not None and running.phase == "proposing" and running.running
    assert running.path == str((tmp_path / "app").resolve()) and running.config == ""
    f.go.set()

    done = settled(f.jobs)
    assert done.phase == "draft" and done.error == ""
    assert done.config.startswith("# Proposed by `cofabriq repo add` for acme/app")
    assert [(c.field, c.found) for c in done.citations][-1] == ("suite sh", False)
    assert f.jobs.repos() == [REPO]


def test_a_proposal_is_refused_for_a_subfolder_or_a_repo_added_already(
    tmp_path: Path,
) -> None:
    f = Jobs(tmp_path / "targets")
    f.go.set()
    with pytest.raises(Refused, match="not the root folder of a git clone"):
        f.jobs.propose(mine(tmp_path) / "tests")
    f.jobs.propose(tmp_path / "app")
    settled(f.jobs)
    with pytest.raises(Refused, match="added already"):
        f.jobs.propose(tmp_path / "app")
    f.jobs.propose(tmp_path / "app", again=True)  # the page's "Propose again"
    assert settled(f.jobs).phase == "draft"


def added(tmp_path: Path, proven: bool = True) -> Jobs:
    f = Jobs(tmp_path / "targets", proven=proven)
    f.go.set()
    f.jobs.propose(mine(tmp_path))
    settled(f.jobs)
    f.go.clear()
    return f


def config(tmp_path: Path, **loop: object) -> str:
    table = rules().model_dump(exclude_none=True) | loop
    clone = {"path": str((tmp_path / "app").resolve()), "branch": "main"}
    return tomli_w.dumps({"clone": clone, "loop": table})


def test_the_check_saves_the_edit_and_shows_its_steps_live(tmp_path: Path) -> None:
    f = added(tmp_path)
    edit = config(tmp_path, setup="corepack enable")
    f.jobs.check(REPO, edit)

    running = f.jobs.status(REPO)
    assert running is not None and running.phase == "checking"
    assert own_target(f.targets, REPO).read_text("utf-8") == edit  # saved first
    assert [(s.name, s.ok) for s in running.steps] == [("install", None)]
    f.go.set()

    done = settled(f.jobs)
    assert done.phase == "proven"
    assert [(s.name, s.ok) for s in done.steps] == [
        ("install", True),
        ("test", True),
        ("canary sh", True),
    ]
    assert onboard.unproven(f.targets, REPO) is None


def test_a_failed_check_shows_why_and_an_edit_takes_the_proof_back(
    tmp_path: Path,
) -> None:
    f = added(tmp_path, proven=False)
    f.go.set()
    f.jobs.check(REPO, config(tmp_path))
    done = settled(f.jobs)
    assert done.phase == "failed"
    assert [s.output for s in done.steps if not s.ok] == ["1 failed"]

    own_target(f.targets, REPO).write_text(config(tmp_path) + "# edited\n")
    assert settled(f.jobs).phase == "draft"


def test_a_failed_check_goes_back_to_the_proposer_twice_at_most(
    tmp_path: Path,
) -> None:
    f = added(tmp_path, proven=False)
    f.go.set()
    with pytest.raises(Refused, match="no failed check"):
        f.jobs.propose(tmp_path / "app", repair=True)  # a draft: nothing failed yet

    for left in [2, 1]:
        f.jobs.check(REPO, config(tmp_path, setup=f"round {left}"))
        failed = settled(f.jobs)
        assert (failed.phase, failed.repairs_left) == ("failed", left)
        f.jobs.propose(tmp_path / "app", repair=True)
        assert settled(f.jobs).phase == "draft"
        sent = f.earlier[-1]
        assert sent is not None and sent.repairs == 3 - left
        assert sent.config == config(tmp_path, setup=f"round {left}")  # as checked
        assert sent.failed == {"test": "1 failed"}

    f.jobs.check(REPO, config(tmp_path))
    assert settled(f.jobs).repairs_left == 0
    with pytest.raises(Refused, match="2 times: edit the file instead"):
        f.jobs.propose(tmp_path / "app", repair=True)


def test_a_target_file_that_does_not_parse_is_neither_saved_nor_checked(
    tmp_path: Path,
) -> None:
    f = added(tmp_path)
    before = own_target(f.targets, REPO).read_text("utf-8")
    with pytest.raises(Refused, match="not TOML"):
        f.jobs.check(REPO, "[loop\n")
    with pytest.raises(Refused, match="isn't added"):
        f.jobs.check("acme/other", config(tmp_path))
    assert own_target(f.targets, REPO).read_text("utf-8") == before
    assert not settled(f.jobs).running


def test_one_job_at_a_time_and_a_dead_one_says_why(tmp_path: Path) -> None:
    f = added(tmp_path)
    f.jobs.check(REPO, config(tmp_path))
    with pytest.raises(Refused, match="busy"):
        f.jobs.check(REPO, config(tmp_path))
    f.go.set()
    settled(f.jobs)

    def broken(repo: str, on_step: onboard.OnStep) -> onboard.Checked:
        raise RuntimeError("podman: no machine")

    f.jobs.prove_fn = broken
    f.jobs.check(REPO, config(tmp_path, setup="x"))
    done = settled(f.jobs)
    assert done.error == "RuntimeError: podman: no machine"
    assert done.phase == "draft"  # the edit is saved, unproven


def test_a_repo_added_on_the_cli_before_shows_up_by_its_clone(tmp_path: Path) -> None:
    f = added(tmp_path)
    onboard.proposal_path(f.targets, REPO).unlink()
    assert f.jobs.repos() == [REPO]


UI = "http://127.0.0.1:8765"
AUTH = {"origin": UI, "authorization": "Bearer t0k"}


def test_the_page_adds_checks_and_then_lists_the_repo(tmp_path: Path) -> None:
    f = Jobs(tmp_path / "targets")
    f.go.set()
    flows = FlowRunner("", lambda repo, spent: None)  # pyright: ignore[reportArgumentType]
    app = build(
        tmp_path / "refine",
        tmp_path / "runs",
        tmp_path / "clones",
        None,
        port=8765,
        write_token="t0k",
        flows=flows,
        targets_dir=f.targets,
        issues_dir=tmp_path / "issues",
        tickets_dir=tmp_path / "tickets",
        setups=f.jobs,
    )
    c = TestClient(app, base_url=UI)
    path = str(mine(tmp_path))

    assert c.post("/api/repos/add", json={"path": path}).status_code == 403  # guard
    got = c.post("/api/repos/add", json={"path": path}, headers=AUTH)
    assert got.json() == {"repo": REPO}
    settled(f.jobs)
    again = c.post("/api/repos/add", json={"path": path}, headers=AUTH)
    assert again.status_code == 409 and "added already" in again.json()["detail"]
    assert c.get("/api/repos").json() == []  # not proven yet: no flows on it

    bad = c.post(f"/api/repos/{REPO}/check", json={"config": "[loop\n"}, headers=AUTH)
    assert bad.status_code == 409 and "not TOML" in bad.json()["detail"]
    url = f"/api/repos/{REPO}/check"
    assert c.post(url, json={"config": config(tmp_path)}, headers=AUTH).is_success
    settled(f.jobs)

    (own,) = c.get("/api/repos/own").json()
    assert (own["repo"], own["phase"]) == (REPO, "proven")
    assert c.get(f"/api/repos/{REPO}/setup").json()["steps"][0]["name"] == "install"
    assert c.get("/api/repos/acme/none/setup").status_code == 404
    (listed,) = c.get("/api/repos").json()  # Issues lists it, with nothing in it yet
    assert (listed["repo"], listed["issues"], listed["tickets"]) == (REPO, 0, 0)
