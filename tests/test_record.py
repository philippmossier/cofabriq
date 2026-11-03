import json
from pathlib import Path

from cofabriq.loop.lessons import collect
from cofabriq.loop.pr import Draft, Risk
from cofabriq.loop.record import RunRecord, write_record
from tests.test_lessons import gated
from tests.test_loop import _result  # pyright: ignore[reportPrivateUsage]


def test_a_flow_record_keeps_the_keys_it_set_and_its_pr_body(tmp_path: Path) -> None:
    g = gated("extra guard")
    record = RunRecord(result=g.result, gate=g, evidence=["1-a.png"])
    risk = Risk(tier="low", files=[])
    record.draft = Draft(title="Fix #1: t", body="Fixes #1.", risk=risk, review=None)
    out = write_record(tmp_path, "o-r-1-stamp", record)
    stored = json.loads(out.read_text("utf-8"))
    assert list(stored) == ["result", "evidence", "gate", "draft"]  # no judge
    assert (tmp_path / "o-r-1-stamp.pr.md").read_text("utf-8") == "Fixes #1."
    assert RunRecord.model_validate(stored) == record


def test_a_run_record_without_a_review_has_no_pr_body(tmp_path: Path) -> None:
    record = RunRecord(result=_result("escalated", 3, []), judge=None, valid=None)
    out = write_record(tmp_path, "o-r-1-stamp", record)
    stored = json.loads(out.read_text("utf-8"))
    assert stored["judge"] is None and "gate" not in stored
    assert not (tmp_path / "o-r-1-stamp.pr.md").exists()


def test_lessons_read_the_gate_of_a_run_record(tmp_path: Path) -> None:
    g = gated("extra guard")
    write_record(tmp_path, "o-r-1-stamp", RunRecord(result=g.result, gate=g))
    (tmp_path / "o-r-2-stamp.json").write_text('{"repo": "o/r"}')  # an older shape
    (signal,) = collect(tmp_path, tmp_path / "lessons", "o/r")
    assert (signal.kind, signal.text) == ("proven finding", "extra guard")
