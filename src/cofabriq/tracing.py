"""Traces of cofabriq's runs as OpenTelemetry spans, exported over OTLP/HTTP.

Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set (`.env` works too): without
a provider the OpenTelemetry API is a no-op, so the spans below cost nothing.
Any OTLP backend takes them, chosen by endpoint and headers alone: Jaeger
from compose (`http://localhost:4318`), or a hosted one (Dynatrace:
`https://<env>.live.dynatrace.com/api/v2/otlp` and the header
`Authorization=Api-Token%20<token>`; its OTLP ingest is HTTP only).

The `gen_ai.*` names (OpenTelemetry's GenAI semantic conventions, still in
development) live only here, so a rename touches one file. Spans carry
models, tokens, cost, turns and timings, never prompt or answer text: issue
text is untrusted and may carry secrets, and the conventions make content
capture opt-in.

A flow paused at a gate resumes in another process, maybe hours later, so
each process gets its own trace, linked back to the trace the flow started
in: its W3C `traceparent` is kept in the checkpoint, as an HTTP hop would
carry it in a header.
"""

from collections.abc import Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from importlib.metadata import version
from urllib.parse import unquote

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes

# the GenAI semantic conventions
OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
AGENT_NAME = "gen_ai.agent.name"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
# cofabriq's own, for what the conventions don't name
COST = "cofabriq.cost_usd"
WHAT = "cofabriq.llm.what"
TOOLS = "cofabriq.agent.tools"
TURNS = "cofabriq.agent.turns"
BUDGET = "cofabriq.agent.budget_usd"

_tracer = trace.get_tracer("cofabriq")
_w3c = TraceContextTextMapPropagator()


def setup(endpoint: str | None, headers: str = "") -> None:
    """Export every span to `endpoint` (an OTLP/HTTP base URL), batched and
    flushed at exit. Without an endpoint nothing is set up; a provider set
    before (an earlier command in the same process, a test's) is kept."""
    if not endpoint or isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    exporter = OTLPSpanExporter(
        endpoint=f"{endpoint.rstrip('/')}/v1/traces",
        headers=parse_headers(headers),
        timeout=5,  # a backend that is down costs at most this at exit
    )
    resource = Resource.create(
        {"service.name": "cofabriq", "service.version": version("cofabriq")}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def parse_headers(raw: str) -> dict[str, str]:
    """`k=v,k2=v2`, values URL-encoded: OTEL_EXPORTER_OTLP_HEADERS' format."""
    pairs = (p.split("=", 1) for p in raw.split(",") if "=" in p)
    return {k.strip(): unquote(v.strip()) for k, v in pairs}


@contextmanager
def span(
    name: str,
    attributes: Attributes = None,
    *,
    link: str = "",
    kind: SpanKind = SpanKind.INTERNAL,
    expected: tuple[type[Exception], ...] = (),
) -> Generator[Span]:
    """A span around the block, a child of the current one. An exception
    marks it failed, unless it is `expected` (LangGraph's interrupt is how a
    gate pauses, not a failure). `link`: a `traceparent` this span
    continues, from another process."""
    with _tracer.start_as_current_span(
        name,
        attributes=attributes,
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


def traceparent() -> str:
    """The current span as a W3C `traceparent`; "" when there is none
    (tracing off)."""
    carrier: dict[str, str] = {}
    _w3c.inject(carrier)
    return carrier.get("traceparent", "")


def _links(parent: str) -> list[Link]:
    if not parent:
        return []
    context = _w3c.extract({"traceparent": parent})
    linked = trace.get_current_span(context).get_span_context()
    return [Link(linked)] if linked.is_valid else []


def chat(model: str, what: str) -> AbstractContextManager[Span]:
    """One OpenAI call (`llm.structured`); `what` names the answer."""
    return span(
        f"chat {model}",
        {OPERATION: "chat", PROVIDER: "openai", REQUEST_MODEL: model, WHAT: what},
        kind=SpanKind.CLIENT,
    )


def embeddings(model: str) -> AbstractContextManager[Span]:
    """One OpenAI embeddings request (a batch of texts)."""
    return span(
        f"embeddings {model}",
        {OPERATION: "embeddings", PROVIDER: "openai", REQUEST_MODEL: model},
        kind=SpanKind.CLIENT,
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
    name: str, model: str, tools: Sequence[str], budget_usd: float
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
    )


def agent_ran(current: Span, model: str, cost_usd: float, turns: int) -> None:
    """How a session ended: the model that ran, its cost and turns (a run
    stopped at its budget or turn cap still cost something)."""
    current.set_attributes({RESPONSE_MODEL: model, COST: cost_usd, TURNS: turns})
