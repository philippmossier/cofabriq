"""Local tickets: their keys, the store, the check before saving, and what
the loop does differently for one (no GitHub link, no `Fixes #`)."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from cofabriq.cli import app
from cofabriq.cli import issues as cli_issues
from cofabriq.config import Settings
from cofabriq.issues import readiness
from cofabriq.issues import tickets as tickets_mod
from cofabriq.issues.model import Issue, IssueRef, key_label, parse_key, ticket_key
from cofabriq.issues.readiness import Evidence
from cofabriq.issues.tickets import check, find, load_ticket, load_tickets, new_ticket
from cofabriq.issues.triage import Fields, IssueType
from cofabriq.llm import Usage
from cofabriq.loop import lessons
from cofabriq.loop.intake import blockers
from cofabriq.loop.pr import RiskRules, draft
from cofabriq.loop.record import RunRecord
from cofabriq.loop.refine import Refined
from cofabriq.loop.run import LoopResult
from cofabriq.map.components import ComponentRules
from tests.builders import REPO, issue, loop_result, refined, spec


def test_a_key_names_github_issues_by_number_and_local_tickets_with_an_l() -> None:
    assert (ticket_key("github", 51), ticket_key("local", 3)) == ("51", "L3")
    assert (parse_key("51"), parse_key("L3")) == (("github", 51), ("local", 3))
    assert (key_label("51"), key_label("L3")) == ("#51", "L3")
    for bad in ["", "L", "l3", "L03", "03", "-1", "3a", "../3", "L3/x", " 3"]:
        with pytest.raises(ValueError, match="not a ticket key"):
            parse_key(bad)


def test_tickets_are_numbered_per_repo_and_stored_apart(tmp_path: Path) -> None:
    first = new_ticket(tmp_path, REPO, "  first  ", " text ")
    second = new_ticket(tmp_path, REPO, "second", "")
    other = new_ticket(tmp_path, "acme/other", "elsewhere", "")

    assert [t.key for t in (first, second, other)] == ["L1", "L2", "L1"]
    assert (first.title, first.body, first.source) == ("first", "text", "local")
    assert (tmp_path / "acme" / "app" / "L2.json").is_file()
    assert [t.key for t in load_tickets(tmp_path, REPO)] == ["L1", "L2"]
    with pytest.raises(ValueError, match="needs a title"):
        new_ticket(tmp_path, REPO, "   ", "")


def test_two_saves_at_once_never_share_a_number(tmp_path: Path) -> None:
    def save(n: int) -> Issue:
        return new_ticket(tmp_path, REPO, f"t{n}", "")

    with ThreadPoolExecutor(max_workers=8) as pool:
        made = list(pool.map(save, range(16)))

    assert sorted(t.number for t in made) == list(range(1, 17))
    assert len(load_tickets(tmp_path, REPO)) == 16


def test_find_reads_a_key_from_the_store_it_names(tmp_path: Path) -> None:
    tickets, issues = tmp_path / "tickets", tmp_path / "issues"
    new_ticket(tickets, REPO, "a local one", "")
    (issues / "acme" / "app").mkdir(parents=True)
    (issues / "acme" / "app" / "1.json").write_text(
        issue(1, "on github").model_dump_json()
    )

    local, github = find(issues, tickets, REPO, "L1"), find(issues, tickets, REPO, "1")
    assert local is not None and github is not None
    assert (local.title, github.title) == ("a local one", "on github")
    assert find(issues, tickets, REPO, "L2") is None
    with pytest.raises(ValueError):
        find(issues, tickets, REPO, "../1")


class Read:
    """The card's field call: a fixed type, every info item quoted with
    `quote` (None: nothing quoted), questions when blocking."""

    def __init__(self, type: IssueType, quote: str | None, blocking: bool = False):
        self.type: IssueType = type
        self.quote = quote
        self.blocking = blocking
        self.seen: list[Issue] = []

    def fields(self, issue: Issue, components: dict[str, str]) -> tuple[Fields, Usage]:
        self.seen.append(issue)
        info = [Evidence(item=i, quote=self.quote) for i in readiness.ITEMS]
        return Fields(
            type=self.type,
            severity=None,
            component=None,
            summary="s",
            repro_steps=[],
            missing_info=["which browser?"],
            blocking=self.blocking,
            info=info if self.type == "bug" else [],
        ), Usage(input_tokens=1000, output_tokens=100, latency_ms=1)


def test_the_check_says_what_is_missing_and_fills_nothing_in() -> None:
    full = check(REPO, "it breaks", "", {}, Read("bug", "it breaks"))
    assert (full.type, full.missing) == ("bug", [])

    gaps = check(REPO, "it breaks", "", {}, Read("bug", "a quote not in it", True))
    assert gaps.missing == [
        f"missing {item}: {text}" for item, text in readiness.ITEMS.items()
    ] + ["open question: which browser?"]

    chore = check(REPO, "bump deps", "", {}, Read("chore", None))
    assert chore.missing == ["reads as a chore: cofabriq takes bugs and features"]


def test_the_check_refuses_a_blank_title_before_any_call() -> None:
    read = Read("bug", None)
    with pytest.raises(ValueError, match="needs a title"):
        check(REPO, "  ", "text", {}, read)
    assert read.seen == []


def test_a_local_tickets_pr_body_names_it_and_closes_nothing() -> None:
    result = loop_result("green", 1, ["src/a.ts"]).model_copy(
        update={"number": 3, "source": "local"}
    )
    pr = draft(
        issue_title="Shift+Enter sends",
        spec=spec("src/a.ts"),
        result=result,
        tests=[],
        risk_rules=RiskRules(),
        components=ComponentRules(),
        review=None,
    )
    assert pr.title == "Fix L3: Shift+Enter sends"
    assert pr.body.startswith("For the local ticket L3 (not on GitHub).\n")
    assert "Fixes #" not in pr.body


def test_a_local_ticket_blocked_by_the_same_number_on_github_is_blocked() -> None:
    local = issue(3, body="Blocked by #3").model_copy(update={"source": "local"})
    assert blockers(local) == [IssueRef(repo=REPO, number=3)]
    github = issue(3, body="Blocked by #3")
    assert blockers(github) == []  # an issue never blocks itself


def test_records_specs_and_rejections_from_before_local_tickets_still_load(
    tmp_path: Path,
) -> None:
    result = json.loads(loop_result("green", 1, []).model_dump_json())
    del result["source"]
    assert LoopResult.model_validate(result).key == "1"
    assert RunRecord.model_validate({"result": result}).result.source == "github"
    stored = json.loads(refined().model_dump_json())
    del stored["source"]
    assert Refined.model_validate(stored).source == "github"
    old = {"repo": REPO, "number": 7, "title": "t", "gate": "plan", "reason": "no"}
    (tmp_path / "acme-app-rejections.jsonl").write_text(json.dumps(old) + "\n")
    lessons.log_rejection(
        tmp_path, issue(2).model_copy(update={"source": "local"}), "bug", "test", "x"
    )

    found = lessons.collect(tmp_path / "runs", tmp_path, REPO)

    assert [s.where for s in found] == ["#7 t", "L2 t"]


@pytest.fixture
def close_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], Result]:
    """`ticket close <key> --repo acme/app` on a tickets dir in tmp_path."""
    settings = Settings(tickets_dir=tmp_path)
    monkeypatch.setattr(cli_issues, "get_settings", lambda: settings)

    def close(key: str) -> Result:
        return CliRunner().invoke(app, ["ticket", "close", key, "--repo", REPO])

    return close


def test_ticket_close_closes_a_local_ticket(
    tmp_path: Path, close_in: Callable[[str], Result]
) -> None:
    made = new_ticket(tmp_path, REPO, "t", "", now=datetime(2026, 1, 1, tzinfo=UTC))

    out = close_in("L1")

    assert out.exit_code == 0, out.output
    assert "L1" in out.output
    closed = load_ticket(tmp_path, REPO, 1)
    assert closed is not None
    assert (closed.state, closed.state_reason) == ("closed", "completed")
    assert closed.closed_at is not None
    assert closed.closed_at == closed.updated_at
    assert closed.closed_at >= made.created_at
    assert closed.created_at == made.created_at


def test_ticket_close_refuses_a_github_key(
    tmp_path: Path, close_in: Callable[[str], Result]
) -> None:
    out = close_in("51")

    assert out.exit_code == 1
    assert out.output.strip()
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


@pytest.mark.parametrize("key", ["../1", "l2", "L01", "L"])
def test_ticket_close_refuses_a_key_of_neither_form(
    tmp_path: Path, close_in: Callable[[str], Result], key: str
) -> None:
    out = close_in(key)

    assert out.exit_code == 1
    assert "not a ticket key (51 or L3)" in out.output
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_ticket_close_says_when_there_is_no_such_ticket(
    tmp_path: Path, close_in: Callable[[str], Result]
) -> None:
    new_ticket(tmp_path, REPO, "t", "")

    out = close_in("L2")

    assert out.exit_code == 1
    assert "L2" in out.output
    assert not (tmp_path / "acme" / "app" / "L2.json").exists()


def test_closing_twice_changes_nothing_the_second_time(
    tmp_path: Path, close_in: Callable[[str], Result]
) -> None:
    new_ticket(tmp_path, REPO, "t", "")
    assert close_in("L1").exit_code == 0
    path = tmp_path / "acme" / "app" / "L1.json"
    before = path.read_bytes()

    again = close_in("L1")

    assert again.exit_code == 0, again.output
    assert "closed already" in again.output
    assert path.read_bytes() == before


def test_closing_one_ticket_leaves_the_others_open(
    tmp_path: Path, close_in: Callable[[str], Result]
) -> None:
    new_ticket(tmp_path, REPO, "first", "")
    new_ticket(tmp_path, REPO, "second", "")

    assert close_in("L1").exit_code == 0

    assert [(t.key, t.state) for t in load_tickets(tmp_path, REPO)] == [
        ("L1", "closed"),
        ("L2", "open"),
    ]


def test_close_ticket_returns_the_ticket_and_whether_this_call_closed_it(
    tmp_path: Path,
) -> None:
    close_ticket: Callable[..., tuple[Issue, bool] | None] = vars(tickets_mod)[
        "close_ticket"
    ]
    new_ticket(tmp_path, REPO, "t", "", now=datetime(2026, 1, 1, tzinfo=UTC))
    at = datetime(2026, 2, 1, tzinfo=UTC)

    first = close_ticket(tmp_path, REPO, 1, now=at)
    later = datetime(2026, 3, 1, tzinfo=UTC)
    second = close_ticket(tmp_path, REPO, 1, now=later)

    assert first is not None and second is not None
    assert (first[0].state, first[0].closed_at, first[0].updated_at, first[1]) == (
        "closed",
        at,
        at,
        True,
    )
    assert (second[0].closed_at, second[1]) == (at, False)
    assert close_ticket(tmp_path, REPO, 2) is None
