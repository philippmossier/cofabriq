"""Result files under `data/`: the time stamp in their names, the stamped
writes of runs and evals, the newest record per key over them, and the
atomic write every cache and index goes through."""

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

STAMP = "%Y%m%dT%H%M%S"  # UTC, so names sort by time


def stamp() -> str:
    return f"{datetime.now(UTC):{STAMP}}"


def write_atomic(path: Path, data: bytes) -> None:
    """A crash mid-write leaves the old file, never half of a new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_stamped[T](
    folder: Path, prefix: str, adapter: TypeAdapter[T], value: T
) -> Path:
    """`<folder>/<prefix>-<stamp>.json`; the path it wrote."""
    out = folder / f"{prefix}-{stamp()}.json"
    write_atomic(out, adapter.dump_json(value, indent=2))
    return out


def newest_by_key[T](
    folder: Path,
    glob: str,
    adapter: TypeAdapter[T],
    keep: Callable[[T], bool] = lambda _: True,
) -> dict[str, T]:
    """Key -> its newest value over the `{key: value}` files `glob` matches
    in `folder` (stamped names sort by time). Newest file first, and a value
    is parsed only if its key is new: superseded entries in an older format
    are never read. A value `keep` refuses never shadows an older one."""
    newest: dict[str, T] = {}
    for path in sorted(folder.glob(glob), reverse=True):
        stored: dict[str, object] = json.loads(path.read_text("utf-8"))
        for key, raw in stored.items():
            if key in newest:
                continue
            value = adapter.validate_python(raw)
            if keep(value):
                newest[key] = value
    return newest
