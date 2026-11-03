"""The terminal side of the gates `run` and `flow` share: the blocker
check before anything is paid, and the reason a rejection asks for."""

import io

import pytest
from typer.testing import CliRunner

from cofabriq.cli import app
from cofabriq.cli import loop as cli_loop
from cofabriq.issues.model import Issue, IssueRef
from tests.builders import REPO, issue


def test_a_no_asks_why_and_enter_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n  touches the wrong file \n"))
    answer = cli_loop._gate_answer("Approve?")  # pyright: ignore[reportPrivateUsage]
    assert (answer.approve, answer.reason) == (False, "touches the wrong file")

    monkeypatch.setattr("sys.stdin", io.StringIO("n\n\n"))
    answer = cli_loop._gate_answer("Approve?")  # pyright: ignore[reportPrivateUsage]
    assert (answer.approve, answer.reason) == (False, "")

    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert cli_loop._gate_answer("Approve?").approve  # pyright: ignore[reportPrivateUsage]


def test_flow_start_refuses_a_blocked_ticket_before_refine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = issue(3, body="Blocked by #2")

    def refresh(*_: object) -> Issue:
        return blocked

    def unread(_: object) -> dict[IssueRef, Issue]:  # #2 unread: counts as open
        return {}

    monkeypatch.setattr(cli_loop, "_refresh_issue", refresh)
    monkeypatch.setattr(cli_loop, "_blocker_issues", unread)

    def no_clone(*_: object) -> str:
        raise AssertionError("refused before the clone is touched")

    monkeypatch.setattr(cli_loop.refine_mod, "update_clone", no_clone)
    out = CliRunner().invoke(app, ["flow", "start", "3", "--repo", REPO])
    assert out.exit_code == 1
    assert "#3 blocked by #2 (still open): start it once" in out.output
