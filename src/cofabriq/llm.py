"""The structured OpenAI call and the cached batch every LLM step shares.

Each step keeps its own `Protocol` (`Judge`, `Reranker`, `Triager`,
`Extractor`) as the test seam; its OpenAI implementation is one `structured`
call. A batch step stores one record per issue in a JSON list file and calls
the LLM only for issues without a usable record (`cached_batch`).
"""

import json
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openai import OpenAI
from pydantic import BaseModel, TypeAdapter

from cofabriq.files import write_atomic

MODEL = "gpt-6-luna"
PRICE_IN = 0.10 / 1_000_000  # USD per token
PRICE_OUT = 0.50 / 1_000_000  # reasoning tokens are billed as output
WORKERS = 8


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    latency_ms: int


class Tokens(Protocol):
    @property
    def input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...


def cost(usage: Tokens) -> float:
    """USD for one call (a `Usage`, or a record that stores its tokens)."""
    return usage.input_tokens * PRICE_IN + usage.output_tokens * PRICE_OUT


def structured[T: BaseModel](
    client: OpenAI, *, instructions: str, input: str, schema: type[T], what: str
) -> tuple[T, Usage]:
    """One call whose answer is parsed into `schema`. `what` names the answer
    in the error raised when there is none ("verdict for #12")."""
    start = time.perf_counter()
    response = client.responses.parse(
        model=MODEL,
        instructions=instructions,
        input=input,
        text_format=schema,
        reasoning={"effort": "low"},
    )
    latency_ms = round((time.perf_counter() - start) * 1000)
    if response.output_parsed is None or response.usage is None:
        raise RuntimeError(f"no {what}")
    usage = Usage(response.usage.input_tokens, response.usage.output_tokens, latency_ms)
    return response.output_parsed, usage


class Record(BaseModel):
    """One issue's stored answer. Subclasses add their fields after these two."""

    number: int
    version: str  # bumped when the prompt or the schema changes


@dataclass(frozen=True)
class Batch:
    sent: int  # sent to the LLM
    cached: int  # a usable record was already stored


def load_records[R: Record](path: Path, model: type[R], version: str) -> list[R]:
    """Stored records of `version`; others are skipped unvalidated (their
    schema may differ, and they count as missing)."""
    if not path.is_file():
        return []
    raw: list[dict[str, object]] = json.loads(path.read_bytes())
    return [model.model_validate(r) for r in raw if r["version"] == version]


def cached_batch[J, R: Record](
    jobs: Iterable[J],
    path: Path,
    model: type[R],
    version: str,
    *,
    number: Callable[[J], int],
    run: Callable[[J], R],
    usable: Callable[[J, R], bool] = lambda _job, _record: True,
    on_done: Callable[[R], None] = lambda _: None,
    workers: int = WORKERS,
) -> tuple[dict[int, R], Batch]:
    """Records for all jobs, calling `run` (in parallel) only for jobs without
    a stored record of this version that is `usable` for them.

    Records are saved even if a call fails halfway, so a rerun only pays for
    what is missing, and records of issues outside this batch are kept
    (another eval set, the CLI).
    """
    jobs = list(jobs)
    stored = {r.number: r for r in load_records(path, model, version)}
    done = {
        n: stored[n]
        for j in jobs
        if (n := number(j)) in stored and usable(j, stored[n])
    }
    todo = [j for j in jobs if number(j) not in done]
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for record in pool.map(run, todo):
                done[record.number] = record
                on_done(record)
    finally:
        kept = sorted((stored | done).values(), key=lambda r: r.number)
        write_atomic(path, TypeAdapter(list[model]).dump_json(kept, indent=2))
    return done, Batch(sent=len(todo), cached=len(jobs) - len(todo))
