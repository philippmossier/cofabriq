"""A run's steps: what the run store writes while a flow runs, the tree the
viewer gets back, the API over it, and the step docs kept in sync."""

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from cofabriq import runsteps, tracing
from cofabriq.loop.graph import ALLOWED, FlowState, GateAnswer, build_graph, invoke
from cofabriq.serve import build
from tests.builders import REPRO, issue
from tests.fakes import EditingAgent, deps, write

DOCS = runsteps.load_docs()


def flow_run(tmp_path: Path) -> tuple[str, FlowState]:
    """A bug flow on fakes, started and approved in two "processes"."""
    editor = EditingAgent(write("src/a.test.sh", REPRO), write("src/a.ts", "fixed\n"))
    d = deps(tmp_path, editor, [])
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED))
    invoke(
        build_graph(d).compile(checkpointer=saver),
        FlowState(issue=issue(3), route="bug"),
        "t",
    )
    graph = build_graph(d).compile(checkpointer=saver)
    invoke(graph, GateAnswer(approve=True), "t")
    state = FlowState.model_validate(
        graph.get_state({"configurable": {"thread_id": "t"}}).values
    )
    return tracing.run_id(state.trace), state


def names(step: runsteps.RunStep) -> list[str]:
    return [c.name for c in step.children]


def test_a_flow_and_its_resume_are_one_run_of_two_roots(
    run_store: tracing.RunStore, tmp_path: Path
) -> None:
    run, state = flow_run(tmp_path)

    tree = runsteps.load_steps(run_store.directory, run, DOCS)

    assert tree is not None
    first, resumed = tree
    assert (first.detail, resumed.detail) == ("started", "resumed with your answer")
    assert names(first) == ["node refine"]  # the paused gate: answered below
    assert resumed.children[0].detail == "you approved"
    assert names(resumed) == [
        "node plan_gate",
        "node setup",
        "node write_test",
        "node build",
        "node review",
        "node record",
        "node finish",
    ]
    assert all(s.status == "ok" for s in resumed.children)
    build_step = resumed.children[3]
    assert build_step.title == DOCS["build"].title
    assert build_step.code is not None
    assert build_step.code.function == "cofabriq.loop.run.build_stage"
    assert build_step.code.url.startswith(f"{runsteps.SOURCE}/blob/c0ffee/src/")
    record = tmp_path / "runs" / f"{state.record}.json"
    assert json.loads(record.read_text())["trace"] == run


def test_a_flow_waiting_at_its_gate_keeps_the_paused_row(
    run_store: tracing.RunStore, tmp_path: Path
) -> None:
    d = deps(tmp_path, EditingAgent(), [])
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED))
    graph = build_graph(d).compile(checkpointer=saver)
    invoke(graph, FlowState(issue=issue(3), route="bug"), "t")
    state = FlowState.model_validate(
        graph.get_state({"configurable": {"thread_id": "t"}}).values
    )

    tree = runsteps.load_steps(run_store.directory, tracing.run_id(state.trace), DOCS)

    assert tree is not None
    [first] = tree
    assert names(first) == ["node refine", "node plan_gate"]
    assert first.children[1].status == "paused"
    assert first.children[1].detail == "paused for your answer"


def test_a_step_keeps_what_it_changed_in_the_state(
    run_store: tracing.RunStore, tmp_path: Path
) -> None:
    run, _ = flow_run(tmp_path)
    tree = runsteps.load_steps(run_store.directory, run, DOCS)
    assert tree is not None
    refine = tree[0].children[0]

    kept = runsteps.load_payload(run_store.directory, run, refine.id)

    assert refine.payload and kept is not None
    update = kept["update"]
    assert isinstance(update, dict) and "refined" in update


def test_a_step_still_running_has_no_end(run_store: tracing.RunStore) -> None:
    with tracing.span(tracing.FLOW) as root, tracing.span("node build"):
        run = format(root.get_span_context().trace_id, "032x")
        tree = runsteps.load_steps(run_store.directory, run, DOCS)

    assert tree is not None
    [flow] = tree
    [build_step] = flow.children
    assert (flow.status, build_step.status, build_step.ms) == (
        "running",
        "running",
        None,
    )


def test_spans_outside_a_flow_are_not_kept(run_store: tracing.RunStore) -> None:
    with tracing.span("cofabriq.triage"), tracing.span("chat gpt-6-luna"):
        pass

    assert not run_store.directory.exists()


def test_a_half_written_line_is_skipped(run_store: tracing.RunStore) -> None:
    with tracing.span(tracing.FLOW) as root:
        run = format(root.get_span_context().trace_id, "032x")
    path = run_store.directory / f"{run}.jsonl"
    path.write_text(path.read_text() + '{"ev": "start", "sp')

    tree = runsteps.load_steps(run_store.directory, run, DOCS)

    assert tree is not None and [s.status for s in tree] == ["ok"]


def test_a_failed_step_shows_why(run_store: tracing.RunStore) -> None:
    run = ""
    with pytest.raises(RuntimeError), tracing.span(tracing.FLOW) as root:
        run = format(root.get_span_context().trace_id, "032x")
        with tracing.span("sandbox.run"):
            raise RuntimeError("podman gone")

    tree = runsteps.load_steps(run_store.directory, run, DOCS)

    assert tree is not None
    [sandbox] = tree[0].children
    assert (sandbox.status, sandbox.error) == ("error", "RuntimeError: podman gone")


def test_the_api_serves_steps_payloads_and_docs(
    run_store: tracing.RunStore, tmp_path: Path
) -> None:
    run, _ = flow_run(tmp_path)
    api = TestClient(
        build(
            tmp_path / "refine",
            tmp_path / "runs",
            tmp_path / "clones",
            None,
            trace_dir=run_store.directory,
        )
    )

    tree = api.get(f"/api/traces/{run}").json()
    steps = tree["steps"]
    refine = steps[0]["children"][0]
    payload = api.get(f"/api/traces/{run}/payloads/{refine['id']}")
    docs = api.get("/api/step-docs").json()

    assert [s["name"] for s in steps] == [tracing.FLOW, tracing.FLOW]
    assert set(tree["totals"]) == {"seconds", "agents", "turns", "cost_usd"}
    assert payload.status_code == 200 and "update" in payload.json()
    assert docs["review"]["code"] == "cofabriq.loop.gate.review_gate"
    assert api.get("/api/traces/../../etc").status_code == 404
    assert api.get(f"/api/traces/{'0' * 32}").status_code == 404
    assert api.get(f"/api/traces/{run}/payloads/nope").status_code == 404


def test_every_step_has_a_doc_naming_a_function_that_exists(tmp_path: Path) -> None:
    graph = build_graph(deps(tmp_path, EditingAgent(), [])).compile()
    span_names = [f"node {n}" for n in graph.nodes if not n.startswith("__")]
    span_names += [tracing.FLOW, "invoke_agent claude-code", "sandbox.run"]
    span_names += ["chat gpt-6-luna", "embeddings text-embedding-3-small"]

    missing = {runsteps.doc_key(n) for n in span_names} - set(DOCS)

    assert missing == set()
    for doc in DOCS.values():
        assert doc.title and doc.summary and doc.why, doc.key
        module, _, attr = doc.code.rpartition(".")
        assert hasattr(importlib.import_module(module), attr), doc.code
