import json
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from cofabriq.files import STAMP, newest_by_key, write_atomic, write_stamped

_ints = TypeAdapter(int)


def test_a_stamped_name_carries_the_time(tmp_path: Path) -> None:
    adapter = TypeAdapter(dict[str, int])
    out = write_stamped(tmp_path / "runs", "eval", adapter, {"a": 1})
    prefix, stamp = out.stem.split("-", 1)
    assert prefix == "eval" and datetime.strptime(stamp, STAMP)
    assert json.loads(out.read_text()) == {"a": 1}


def test_the_newest_file_wins_per_key(tmp_path: Path) -> None:
    (tmp_path / "eval-20261018T100000.json").write_text('{"a": 1, "b": 1}')
    (tmp_path / "eval-20261018T110000.json").write_text('{"a": 2}')
    (tmp_path / "other-20261018T120000.json").write_text('{"a": 3}')
    assert newest_by_key(tmp_path, "eval-*.json", _ints) == {"a": 2, "b": 1}


def test_a_superseded_entry_is_never_parsed(tmp_path: Path) -> None:
    (tmp_path / "eval-20261018T100000.json").write_text('{"a": "an old format"}')
    (tmp_path / "eval-20261018T110000.json").write_text('{"a": 2}')
    assert newest_by_key(tmp_path, "eval-*.json", _ints) == {"a": 2}


def test_a_refused_value_never_shadows_an_older_one(tmp_path: Path) -> None:
    (tmp_path / "eval-20261018T100000.json").write_text('{"a": 1}')
    (tmp_path / "eval-20261018T110000.json").write_text('{"a": 0}')
    assert newest_by_key(tmp_path, "eval-*.json", _ints, keep=bool) == {"a": 1}


def test_an_atomic_write_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "verdicts.json"
    write_atomic(path, b"[1]")
    write_atomic(path, b"[2]")
    assert path.read_bytes() == b"[2]"
    assert [p.name for p in path.parent.iterdir()] == ["verdicts.json"]
