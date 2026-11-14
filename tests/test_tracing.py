"""The spans a run leaves: names and attributes per the GenAI conventions,
parents across threads, and a resumed flow linked to its first trace."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from openai import OpenAI
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, StatusCode
from pydantic import BaseModel

from cofabriq import llm, tracing
from cofabriq.issues.model import Issue
from cofabriq.issues.triage import Fields, triage
from cofabriq.issues.verdict import Answer, Candidate
from cofabriq.llm import Usage
from cofabriq.loop.engine import AgentError, ApiKey, ClaudeCode
from cofabriq.loop.graph import ALLOWED, FlowState, GateAnswer, build_graph, invoke
from cofabriq.loop.sandbox import Podman
from tests.builders import context, fields, issue
from tests.fakes import EditingAgent, FakeJudge, FakeTriager, deps
from tests.test_kill_switch import agent


def named(spans: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.name == name]


def one(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    [found] = named(spans, name)
    return found


def ids(span: ReadableSpan) -> SpanContext:
    assert span.context is not None  # set on every finished span
    return span.context


class Out(BaseModel):
    answer: str


class _Tokens:
    input_tokens = 1000
    output_tokens = 100


class _Response:
    output_parsed = Out(answer="a")
    usage = _Tokens()
    model = "gpt-6-luna-2026-10-01"


class _Responses:
    def parse(self, **_: object) -> _Response:
        return _Response()


class _Client:
    responses = _Responses()


def test_a_structured_call_is_a_chat_span_with_tokens_and_cost(
    spans: InMemorySpanExporter,
) -> None:
    client = cast(OpenAI, _Client())

    llm.structured(client, instructions="i", input="secret", schema=Out, what="#12")

    span = one(spans, f"chat {llm.MODEL}")
    assert dict(span.attributes or {}) == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": llm.MODEL,
        "gen_ai.response.model": "gpt-6-luna-2026-10-01",
        "gen_ai.usage.input_tokens": 1000,
        "gen_ai.usage.output_tokens": 100,
        "cofabriq.cost_usd": pytest.approx(1000 * 0.10e-6 + 100 * 0.50e-6),
        "cofabriq.llm.what": "#12",
    }  # no prompt text: "secret" is in no attribute


def test_an_agent_session_is_one_span_with_cost_and_turns(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    agent(tmp_path, cost=0.4)("prompt", {"type": "object"}, tmp_path)

    attributes = one(spans, "invoke_agent claude-code").attributes or {}
    assert attributes["gen_ai.operation.name"] == "invoke_agent"
    assert attributes["gen_ai.request.model"] == "claude-opus-5-5"
    assert attributes["cofabriq.agent.tools"] == ("Read", "Grep", "Glob")
    assert (attributes["cofabriq.cost_usd"], attributes["cofabriq.agent.turns"]) == (
        0.4,
        2,
    )


def test_a_failed_session_is_an_error_span_that_still_shows_its_cost(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    answer = {"subtype": "error_max_budget_usd", "total_cost_usd": 1.0}
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\necho '{json.dumps(answer)}'\n")
    script.chmod(0o755)
    failing = ClaudeCode(auth=ApiKey("k"), binary=str(script), stop_file=tmp_path / "S")

    with pytest.raises(AgentError):
        failing("prompt", {"type": "object"}, tmp_path)

    span = one(spans, "invoke_agent claude-code")
    assert span.status.status_code == StatusCode.ERROR
    assert (span.attributes or {})["cofabriq.cost_usd"] == 1.0


def test_a_sandbox_command_is_a_span_and_red_is_no_error(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    Podman(image="node:24-slim", binary="false")("pnpm test", tmp_path, network=False)

    span = one(spans, "sandbox.run")
    assert dict(span.attributes or {}) == {
        "cofabriq.sandbox.command": "pnpm test",
        "cofabriq.sandbox.image": "node:24-slim",
        "cofabriq.sandbox.network": False,
        "cofabriq.sandbox.ok": False,
    }
    assert span.status.status_code == StatusCode.UNSET


class SpanTriager(FakeTriager):
    def fields(self, issue: Issue, components: dict[str, str]) -> tuple[Fields, Usage]:
        with tracing.span("card"):
            return super().fields(issue, components)


class SpanJudge(FakeJudge):
    def judge(
        self, query: Issue, candidates: Sequence[Candidate]
    ) -> tuple[Answer, Usage]:
        with tracing.span("verdict"):
            return super().judge(query, candidates)


def test_triages_two_threads_keep_their_parent(spans: InMemorySpanExporter) -> None:
    answer = Answer(verdict="none", target=None, evidence="", problem=None)

    with tracing.span("cofabriq.triage"):
        triage(context(), SpanTriager(fields()), SpanJudge(answer))

    root = one(spans, "cofabriq.triage")
    for name in ("card", "verdict"):
        parent = one(spans, name).parent
        assert parent is not None and parent.span_id == ids(root).span_id


def test_a_resumed_flow_is_a_new_trace_linked_to_the_first(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    d = deps(tmp_path, EditingAgent(), [])
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED))

    first = build_graph(d).compile(checkpointer=saver)
    invoke(first, FlowState(issue=issue(3), route="bug"), "t")
    start = one(spans, "cofabriq.flow")
    paused = named(spans, "node plan_gate")[0]
    assert (paused.attributes or {})["cofabriq.paused"] is True
    assert paused.status.status_code == StatusCode.UNSET  # a gate, no failure
    spans.clear()

    second = build_graph(d).compile(checkpointer=saver)  # "another process"
    invoke(second, GateAnswer(approve=False), "t")

    resumed = one(spans, "cofabriq.flow")
    assert ids(resumed).trace_id != ids(start).trace_id
    [link] = resumed.links
    assert link.context.trace_id == ids(start).trace_id
    assert {s.name for s in spans.get_finished_spans()} == {
        "cofabriq.flow",
        "node plan_gate",
        "node finish",
    }


def test_setup_keeps_a_provider_already_set() -> None:
    before = trace.get_tracer_provider()

    tracing.setup(None)
    tracing.setup("http://localhost:4318")  # a second command in one process

    assert trace.get_tracer_provider() is before


def test_headers_parse_like_the_otel_variable() -> None:
    raw = "Authorization=Api-Token%20dt0c01.abc, x-team = a=b"

    assert tracing.parse_headers(raw) == {
        "Authorization": "Api-Token dt0c01.abc",
        "x-team": "a=b",
    }
