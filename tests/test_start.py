"""Starting a flow from the browser: the issue list, the runner's start in a
background thread, and the refusals that come back at once."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cofabriq.issues.fetch import fetch_issues
from cofabriq.issues.model import Issue
from cofabriq.loop.graph import FlowRunner, FlowStatus, Refused
from cofabriq.serve import build
from tests.builders import REPO, issue
from tests.fakes import EditingAgent, deps
from tests.test_issues import ListSource

LOOP = """[loop]
image = "node:24-slim"
install = "true"
repro_dir = "src"
repro = "sh"
checks = []
"""


def runner(tmp_path: Path, dsn: str, prepare: "PrepareFake") -> FlowRunner:
    d = deps(tmp_path, EditingAgent(), [])
    return FlowRunner(dsn, lambda repo, spent: d, prepare=prepare)


class PrepareFake:
    """`prepare` without GitHub or a clone; records what it was asked."""

    def __init__(self, refuse: str = "") -> None:
        self.refuse = refuse
        self.asked: list[int] = []

    def __call__(self, repo: str, number: int) -> Issue:
        self.asked.append(number)
        if self.refuse:
            raise Refused(self.refuse)
        return issue(number)


def settled(r: FlowRunner, thread: str) -> FlowStatus:
    """The flow once its background work is done."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = r.status(thread)
        if found is not None and not found.running:
            return found
        time.sleep(0.05)
    raise AssertionError(f"{thread} still running")


def test_a_started_flow_refines_in_the_background_and_waits_at_the_plan_gate(
    tmp_path: Path, pg_dsn: str
) -> None:
    prepare = PrepareFake()
    r = runner(tmp_path, pg_dsn, prepare)

    thread = r.start(REPO, 3, "bug")

    assert thread in r.threads()  # listed before its first checkpoint
    done = settled(r, thread)
    assert (done.waiting, done.error) == ("plan", "")
    assert done.state.refined is not None
    with pytest.raises(Refused, match="has a flow already"):
        r.start(REPO, 3, "bug")
    assert prepare.asked == [3]  # the second start never got that far


def test_what_prepare_refuses_starts_nothing(tmp_path: Path, pg_dsn: str) -> None:
    r = runner(tmp_path, pg_dsn, PrepareFake(refuse="#4 is blocked by #2"))

    with pytest.raises(Refused, match="blocked by #2"):
        r.start(REPO, 4, "bug")

    assert r.status(f"{REPO}#4") is None


def store(tmp_path: Path, *issues: Issue) -> Path:
    """An issue store and a target file with `[loop]` for REPO."""
    source = ListSource(list(issues))
    source.repo = REPO
    fetch_issues(source, tmp_path / "issues")
    (tmp_path / "targets").mkdir(exist_ok=True)
    (tmp_path / "targets" / f"{REPO.replace('/', '-')}.toml").write_text(LOOP)
    return tmp_path


def api(tmp_path: Path, flows: FlowRunner | None = None) -> TestClient:
    return TestClient(
        build(
            tmp_path,
            tmp_path,
            tmp_path,
            None,
            flows=flows,
            issues_dir=tmp_path / "issues",
            targets_dir=tmp_path / "targets",
        )
    )


def test_the_issue_list_is_the_store_of_a_runnable_repo(tmp_path: Path) -> None:
    c = api(store(tmp_path, issue(1, "old"), issue(2, "new", labels=["bug"])))

    assert c.get("/api/repos").json() == [{"repo": REPO, "issues": 2}]
    rows = c.get(f"/api/issues/{REPO}").json()
    assert [(r["number"], r["title"], r["flow"]) for r in rows] == [
        (2, "new", False),
        (1, "old", False),
    ]
    assert rows[0]["url"] == f"https://github.com/{REPO}/issues/2"
    # a repo without `[loop]` is no repo to start flows on
    assert c.get("/api/issues/other/repo").status_code == 404


def test_start_answers_409_with_the_reason(tmp_path: Path, pg_dsn: str) -> None:
    flows = runner(tmp_path, pg_dsn, PrepareFake(refuse="#4 is blocked by #2"))
    c = api(store(tmp_path, issue(4)), flows)

    got = c.post(f"/api/flows/{REPO}/4/start", json={"route": "bug"})

    assert got.status_code == 409
    assert got.json() == {"detail": "#4 is blocked by #2"}
    bad = c.post(f"/api/flows/{REPO}/4/start", json={"route": "question"})
    assert bad.status_code == 422  # only the loop's routes
