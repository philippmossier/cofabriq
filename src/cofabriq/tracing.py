"""Traces of cofabriq's runs as OpenTelemetry spans: kept on disk per run for
the UI's step view, and exported over OTLP/HTTP when an endpoint is set.

Two consumers of the same spans:
- `RunStore` (always on in the CLI): every span under a flow is appended to
  `data/traces/<run>.jsonl` as it starts and as it ends, so the viewer
  shows a step while it still runs (`runsteps.py` reads it back). What a
  step got and gave (prompt, answer, command output: `capture`) goes into
  `data/traces/<run>/<span>.json` next to it, local only.
- OTLP, only with `OTEL_EXPORTER_OTLP_ENDPOINT` (`.env` works too): Jaeger
  from compose (`http://localhost:4318`), or a hosted backend by endpoint
  and headers alone (Dynatrace: `https://<env>.live.dynatrace.com/api/v2/otlp`
  and the header `Authorization=Api-Token%20<token>`; its OTLP ingest is
  HTTP only). The exported spans carry models, tokens, cost, turns and
  timings, never prompt or answer text: issue text is untrusted and may
  carry secrets, and the conventions make content capture opt-in.

Without a provider (tests set their own) the OpenTelemetry API is a no-op.
The `gen_ai.*` names (OpenTelemetry's GenAI semantic conventions, still in
development) live only here, so a rename touches one file.

A flow paused at a gate resumes in another process, maybe hours later, so
each process gets its own trace, linked back to the trace the flow started
in: its W3C `traceparent` is kept in the checkpoint, as an HTTP hop would
carry it in a header. The first trace's id is the run's id in the store.
"""

import subprocess
import threading
from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import CodeType
from urllib.parse import unquote

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes
from pydantic_core import to_json

from cofabriq.git import git

# the GenAI semantic conventions
OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
AGENT_NAME = "gen_ai.agent.name"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
# the code conventions: the function a span stands for
CODE_FUNCTION = "code.function.name"
CODE_FILE = "code.file.path"
CODE_LINE = "code.line.number"
# cofabriq's own, for what the conventions don't name
COST = "cofabriq.cost_usd"
WHAT = "cofabriq.llm.what"
TOOLS = "cofabriq.agent.tools"
TURNS = "cofabriq.agent.turns"
BUDGET = "cofabriq.agent.budget_usd"
APPROVED = "cofabriq.gate.approved"  # a gate's answer; the reason stays out

FLOW = "cofabriq.flow"  # the root of one process's part of a run
ROOT = (
    Path(__file__).resolve().parents[2]
)  # the checkout: code paths are relative to it

_tracer = trace.get_tracer("cofabriq")
_w3c = TraceContextTextMapPropagator()


def setup(
    endpoint: str | None, headers: str = "", trace_dir: Path | None = None
) -> None:
    """Keep the spans of every run in `trace_dir` and export them to
    `endpoint` (an OTLP/HTTP base URL), batched and flushed at exit. With
    neither nothing is set up; a provider set before (an earlier command in
    the same process, a test's) is kept."""
    if not endpoint and trace_dir is None:
        return
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    resource = Resource.create(
        {"service.name": "cofabriq", "service.version": version("cofabriq")}
    )
    provider = TracerProvider(resource=resource)
    if trace_dir is not None:
        use_store(provider, RunStore(trace_dir, _commit()))
    if endpoint:
        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces",
            headers=parse_headers(headers),
            timeout=5,  # a backend that is down costs at most this at exit
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def parse_headers(raw: str) -> dict[str, str]:
    """`k=v,k2=v2`, values URL-encoded: OTEL_EXPORTER_OTLP_HEADERS' format."""
    pairs = (p.split("=", 1) for p in raw.split(",") if "=" in p)
    return {k.strip(): unquote(v.strip()) for k, v in pairs}


def _commit() -> str:
    """The checkout's commit, for code links at the version that ran; ""
    outside a git checkout."""
    try:
        return git(ROOT, "rev-parse", "HEAD").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# ---- the run store: what the UI's step view reads ---------------------------


class RunStore(SpanProcessor):
    """Every span under a `cofabriq.flow` root, appended to
    `<directory>/<run>.jsonl` once when it starts and once when it ends
    (a start without an end is a step still running). `<run>` is the
    flow's first trace id: a resumed flow's root links to it. Spans outside
    a flow (triage, the evals) are not kept. Two processes (`flow start`,
    then `serve`) append to the same file; a line is one small write.

    A failed write is dropped: the store is a view of the run, never a
    reason for the run to fail."""

    def __init__(self, directory: Path, commit: str = "") -> None:
        self.directory = directory
        self.commit = commit  # in each root's start line, for code links
        self._runs: dict[int, str] = {}  # trace id -> run id
        self._payloads: dict[int, dict[str, object]] = {}  # span id -> captured
        self._lock = threading.Lock()

    def on_start(self, span: SdkSpan, parent_context: Context | None = None) -> None:
        ids = span.get_span_context()
        with self._lock:
            if span.name == FLOW:
                first = [k.context.trace_id for k in span.links if k.context.is_valid]
                self._runs[ids.trace_id] = _hex(first[0] if first else ids.trace_id, 32)
            run = self._runs.get(ids.trace_id)
            if run is None:
                return
            line: dict[str, object] = {
                "ev": "start",
                "span": _hex(ids.span_id),
                "parent": _hex(span.parent.span_id) if span.parent else None,
                "name": span.name,
                "at": _iso(span.start_time),
                "attrs": dict(span.attributes or {}),
            }
            if span.name == FLOW:
                line["commit"] = self.commit
            self._append(run, line)

    def on_end(self, span: ReadableSpan) -> None:
        ids = span.context
        if ids is None:
            return
        with self._lock:
            run = self._runs.get(ids.trace_id)
            payload = self._payloads.pop(ids.span_id, None)
            if run is None:
                return
            if payload:
                self._save(run, _hex(ids.span_id), payload)
            start, end = span.start_time or 0, span.end_time or 0
            failed = span.status.status_code == StatusCode.ERROR
            self._append(
                run,
                {
                    "ev": "end",
                    "span": _hex(ids.span_id),
                    "at": _iso(span.end_time),
                    "ms": round((end - start) / 1e6),
                    "status": "error" if failed else "ok",
                    "error": (span.status.description or "") if failed else "",
                    "attrs": dict(span.attributes or {}),
                    "payload": bool(payload),
                },
            )
            if span.name == FLOW:  # this process's part of the run is over
                del self._runs[ids.trace_id]

    def capture(self, current: Span, values: dict[str, object]) -> None:
        ids = current.get_span_context()
        with self._lock:
            if ids.trace_id in self._runs:
                self._payloads.setdefault(ids.span_id, {}).update(values)

    def _append(self, run: str, line: dict[str, object]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / f"{run}.jsonl").open("ab") as f:
                f.write(to_json(line, fallback=str) + b"\n")
        except OSError:
            pass

    def _save(self, run: str, span: str, payload: dict[str, object]) -> None:
        try:
            folder = self.directory / run
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{span}.json").write_bytes(
                to_json(payload, indent=2, fallback=str)
            )
        except OSError:
            pass


_store: RunStore | None = None


def use_store(provider: TracerProvider, store: RunStore) -> None:
    """Keep the runs `provider` traces in `store` (`setup`, and the tests)."""
    global _store
    provider.add_span_processor(store)
    _store = store


def capture(current: Span, **values: object) -> None:
    """What a step got and gave (a prompt, an answer, a command's output),
    for the local run store only: never an attribute, so never exported."""
    if _store is not None:
        _store.capture(current, values)


def _hex(n: int, width: int = 16) -> str:
    return format(n, f"0{width}x")


def _iso(ns: int | None) -> str:
    return datetime.fromtimestamp((ns or 0) / 1e9, UTC).isoformat()


# ---- spans --------------------------------------------------------------------


@contextmanager
def span(
    name: str,
    attributes: Attributes = None,
    *,
    link: str = "",
    kind: SpanKind = SpanKind.INTERNAL,
    expected: tuple[type[Exception], ...] = (),
    code: Callable[..., object] | None = None,
) -> Generator[Span]:
    """A span around the block, a child of the current one. An exception
    marks it failed, unless it is `expected` (LangGraph's interrupt is how a
    gate pauses, not a failure). `link`: a `traceparent` this span
    continues, from another process. `code`: the function the span stands
    for (its name, file and line, for the viewer's code links)."""
    with _tracer.start_as_current_span(
        name,
        attributes={**(attributes or {}), **code_of(code)} if code else attributes,
        links=_links(link),
        kind=kind,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except expected:
            raise
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise


def code_of(fn: Callable[..., object]) -> dict[str, str | int]:
    """Where `fn` is defined: module-qualified name, file (relative to the
    checkout) and first line, read from the function object itself."""
    code = getattr(fn, "__code__", None)
    if not isinstance(code, CodeType):
        return {}
    path = Path(code.co_filename).resolve()
    module, name = getattr(fn, "__module__", ""), getattr(fn, "__qualname__", "")
    return {
        CODE_FUNCTION: f"{module}.{name}",
        CODE_FILE: str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path),
        CODE_LINE: code.co_firstlineno,
    }


def traceparent() -> str:
    """The current span as a W3C `traceparent`; "" when there is none
    (tracing off)."""
    carrier: dict[str, str] = {}
    _w3c.inject(carrier)
    return carrier.get("traceparent", "")


def run_id(parent: str) -> str:
    """The trace id in a `traceparent` (`00-<trace>-<span>-<flags>`): the
    run's id in the store; "" for none."""
    parts = parent.split("-")
    return parts[1] if len(parts) == 4 else ""


def _links(parent: str) -> list[Link]:
    if not parent:
        return []
    context = _w3c.extract({"traceparent": parent})
    linked = trace.get_current_span(context).get_span_context()
    return [Link(linked)] if linked.is_valid else []


def chat(
    model: str, what: str, code: Callable[..., object] | None = None
) -> AbstractContextManager[Span]:
    """One OpenAI call (`llm.structured`); `what` names the answer."""
    return span(
        f"chat {model}",
        {OPERATION: "chat", PROVIDER: "openai", REQUEST_MODEL: model, WHAT: what},
        kind=SpanKind.CLIENT,
        code=code,
    )


def embeddings(
    model: str, code: Callable[..., object] | None = None
) -> AbstractContextManager[Span]:
    """One OpenAI embeddings request (a batch of texts)."""
    return span(
        f"embeddings {model}",
        {OPERATION: "embeddings", PROVIDER: "openai", REQUEST_MODEL: model},
        kind=SpanKind.CLIENT,
        code=code,
    )


def used(
    current: Span, model: str, input_tokens: int, output_tokens: int, cost_usd: float
) -> None:
    """What a call answered with: the model that ran, its tokens and cost."""
    current.set_attributes(
        {
            RESPONSE_MODEL: model,
            INPUT_TOKENS: input_tokens,
            OUTPUT_TOKENS: output_tokens,
            COST: cost_usd,
        }
    )


def embedded(current: Span, model: str, input_tokens: int) -> None:
    """What an embeddings request answered with (no price kept: a cent per
    hundred issues)."""
    current.set_attributes({RESPONSE_MODEL: model, INPUT_TOKENS: input_tokens})


def agent(
    name: str,
    model: str,
    tools: Sequence[str],
    budget_usd: float,
    code: Callable[..., object] | None = None,
) -> AbstractContextManager[Span]:
    """One coding agent session (`engine.ClaudeCode`): many model calls and
    tool uses inside one subprocess, seen from outside as one span."""
    return span(
        f"invoke_agent {name}",
        {
            OPERATION: "invoke_agent",
            PROVIDER: "anthropic",
            AGENT_NAME: name,
            REQUEST_MODEL: model,
            TOOLS: list(tools),
            BUDGET: budget_usd,
        },
        kind=SpanKind.CLIENT,
        code=code,
    )


def answered(approved: bool) -> None:
    """The dev's answer at a gate, on the gate node's span."""
    trace.get_current_span().set_attribute(APPROVED, approved)


def agent_ran(current: Span, model: str, cost_usd: float, turns: int) -> None:
    """How a session ended: the model that ran, its cost and turns (a run
    stopped at its budget or turn cap still cost something)."""
    current.set_attributes({RESPONSE_MODEL: model, COST: cost_usd, TURNS: turns})
