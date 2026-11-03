import json
from pathlib import Path

import pytest

from cofabriq import llm
from cofabriq.llm import Batch


class Answer(llm.Record):
    text: str


class Calls:
    """Answers each job with its number in words; fails on one number."""

    def __init__(self, fail: int | None = None) -> None:
        self.seen: list[int] = []
        self.fail = fail

    def __call__(self, job: int) -> Answer:
        if job == self.fail:
            raise RuntimeError("API down")
        self.seen.append(job)
        return Answer(number=job, version="v2", text=f"#{job}")


def batch(
    jobs: list[int], path: Path, calls: Calls, usable: bool = True
) -> tuple[dict[int, Answer], Batch]:
    return llm.cached_batch(
        jobs,
        path,
        Answer,
        "v2",
        number=lambda j: j,
        run=calls,
        usable=lambda _j, _r: usable,
        workers=2,
    )


def test_a_rerun_only_calls_for_what_is_missing(tmp_path: Path) -> None:
    path, calls = tmp_path / "cache.json", Calls()

    batch([1, 2], path, calls)
    done, counts = batch([1, 2, 3], path, calls)

    assert sorted(calls.seen) == [1, 2, 3]
    assert counts == Batch(sent=1, cached=2)
    assert {n: r.text for n, r in done.items()} == {1: "#1", 2: "#2", 3: "#3"}


def test_records_of_another_version_count_as_missing(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    old = {"number": 1, "version": "v1", "answer": "other schema"}
    path.write_text(json.dumps([old]))
    calls = Calls()

    batch([1], path, calls)

    assert calls.seen == [1]
    [record] = llm.load_records(path, Answer, "v2")
    assert record.text == "#1"


def test_a_record_not_usable_for_its_job_is_asked_again(tmp_path: Path) -> None:
    path, calls = tmp_path / "cache.json", Calls()

    batch([1], path, calls)
    _, counts = batch([1], path, calls, usable=False)

    assert calls.seen == [1, 1] and counts == Batch(sent=1, cached=0)


def test_a_failed_call_keeps_what_was_done_and_what_was_stored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache.json"
    batch([7], path, Calls())  # another eval set, outside the next batch

    with pytest.raises(RuntimeError):
        batch([1, 2], path, Calls(fail=2))

    kept = llm.load_records(path, Answer, "v2")
    assert [r.number for r in kept] == [1, 7]
