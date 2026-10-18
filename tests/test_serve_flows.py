from pathlib import Path

from fastapi.testclient import TestClient

from cofabriq.loop.graph import FlowRunner, FlowState, FlowStatus
from cofabriq.serve import build
from tests.test_dedupe import REPO, issue

THREAD = f"{REPO}#3"
UI = "http://127.0.0.1:8765"


class FakeRunner(FlowRunner):
    """One flow waiting at the plan gate; answers are recorded, not run."""

    def __init__(self) -> None:
        super().__init__("", lambda repo, spent: None)  # pyright: ignore[reportArgumentType]
        self.answers: list[tuple[str, bool]] = []
        self.waiting: bool = True

    def threads(self) -> list[str]:
        return [THREAD]

    def status(self, thread: str) -> FlowStatus | None:
        if thread != THREAD:
            return None
        state = FlowState(issue=issue(3, title="Broken"), route="bug")
        return FlowStatus(
            thread=thread, state=state, waiting="plan" if self.waiting else None
        )

    def answer(self, thread: str, approve: bool) -> bool:
        if not self.waiting:
            return False
        self.answers.append((thread, approve))
        return True


def client(tmp_path: Path, runner: FakeRunner, token: str | None = "t0k") -> TestClient:
    app = build(
        tmp_path, tmp_path, tmp_path, None, port=8765, write_token=token, flows=runner
    )
    return TestClient(app, base_url=UI)


def answer(c: TestClient, **headers: str) -> int:
    base = {"origin": UI, "authorization": "Bearer t0k"}
    url = f"/api/flows/{REPO}/3/answer"
    return c.post(url, json={"approve": True}, headers=base | headers).status_code


def test_flows_are_listed_and_shown(tmp_path: Path) -> None:
    c = client(tmp_path, FakeRunner())
    (f,) = c.get("/api/flows").json()
    assert (f["number"], f["waiting"], f["outcome"]) == (3, "plan", "waiting")
    view = c.get(f"/api/flows/{REPO}/3").json()
    assert view["title"] == "Broken" and view["criteria"] == []
    assert c.get(f"/api/flows/{REPO}/9").status_code == 404
    assert c.get("/api/flows/..%2F..%2Fetc/x/3").status_code == 404


def test_an_answer_needs_the_token_and_a_waiting_gate(tmp_path: Path) -> None:
    runner = FakeRunner()
    c = client(tmp_path, runner)
    assert answer(c, authorization="Bearer wrong") == 401
    assert answer(c, origin="https://evil.example") == 403
    assert answer(c) == 200 and runner.answers == [(THREAD, True)]
    runner.waiting = False
    assert answer(c) == 409


def test_without_a_token_the_gate_is_read_only(tmp_path: Path) -> None:
    c = client(tmp_path, FakeRunner(), token=None)
    assert c.get("/api/flows").status_code == 200
    assert answer(c) == 405
