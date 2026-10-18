"""SPIKE: the LangGraph flow pauses at its gates and resumes with a new
graph object over the same checkpoints (as a new process would)."""

from dataclasses import replace
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command

from cofabriq.loop.engine import AgentRun
from cofabriq.loop.graph import ALLOWED, Deps, FlowState, build_graph
from cofabriq.loop.refine import Spec
from tests.test_dedupe import issue
from tests.test_loop import AGENT_RULES, REPRO, EditingAgent, HostSandbox, setup, write
from tests.test_pr import Reviewer, reviewed
from tests.test_refine import FakeAgent, spec


class Reader:
    """The read-only agent: a spec when asked to refine, else a review."""

    def __init__(self, s: Spec) -> None:
        self.refiner, self.reviewer = FakeAgent(s), Reviewer(reviewed())

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        if schema == Spec.model_json_schema():
            return self.refiner(prompt, schema, cwd)
        return self.reviewer(prompt, schema, cwd)


def deps(tmp_path: Path, editor: EditingAgent, criteria: list[str]) -> Deps:
    clone, _, _ = setup(tmp_path)
    s: Spec = spec("src/a.ts").model_copy(update={"criteria": criteria})
    return Deps(
        clone=clone,
        work_dir=tmp_path / "work",
        rules=AGENT_RULES,
        reader=Reader(s),
        editor=editor,
        sandbox=HostSandbox(),
    )


def test_feature_flow_pauses_twice_and_resumes_in_a_new_graph(tmp_path: Path) -> None:
    editor = EditingAgent(write("src/a.test.sh", REPRO), write("src/a.ts", "fixed\n"))
    d = deps(tmp_path, editor, ["a.ts says fixed"])
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED))
    config: RunnableConfig = {"configurable": {"thread_id": "t"}}

    first = build_graph(d).compile(checkpointer=saver)
    first.invoke(FlowState(issue=issue(3), route="feature"), config)
    assert first.get_state(config).next == ("plan_gate",)

    second = build_graph(d).compile(checkpointer=saver)  # "another process"
    second.invoke(Command(resume=True), config)
    snap = second.get_state(config)
    assert snap.next == ("test_gate",)
    assert "+grep -q fixed" in snap.tasks[0].interrupts[0].value["diff"]
    assert editor.roles == ["reproduce"]  # the builder hasn't run yet

    third = build_graph(d).compile(checkpointer=saver)
    third.invoke(Command(resume=True), config)
    state = FlowState.model_validate(third.get_state(config).values)
    assert state.result is not None and state.result.status == "green"
    assert state.gated is not None and state.gated.status == "approved"
    assert editor.roles == ["reproduce", "implement"]
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").iterdir())


def test_a_rejected_plan_ends_the_flow(tmp_path: Path) -> None:
    d = deps(tmp_path, EditingAgent(), [])
    graph = build_graph(d).compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "t"}}
    graph.invoke(FlowState(issue=issue(3), route="bug"), config)
    graph.invoke(Command(resume=False), config)
    state = FlowState.model_validate(graph.get_state(config).values)
    assert (state.stage, state.note) == ("rejected", "plan rejected")
    assert not graph.get_state(config).next


def test_a_paused_flow_keeps_what_it_spent(tmp_path: Path) -> None:
    from cofabriq.loop.engine import Guard

    guard = Guard(3.0)

    class Booking(Reader):  # books its cost on the guard, as Guarded does
        def __call__(
            self, prompt: str, schema: dict[str, object], cwd: Path
        ) -> AgentRun:
            run = super().__call__(prompt, schema, cwd)
            guard.add(run.cost_usd)
            return run

    d = deps(tmp_path, EditingAgent(), [])
    d = replace(d, reader=Booking(spec("src/a.ts")), guard=guard)
    graph = build_graph(d).compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "t"}}
    graph.invoke(FlowState(issue=issue(3), route="bug"), config)
    state = FlowState.model_validate(graph.get_state(config).values)
    assert state.spent_usd == 0.01  # refine's cost, there for the next process
