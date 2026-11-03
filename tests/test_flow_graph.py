"""The core loop's graph: the flow pauses at its gates and resumes with a
new graph object over the same checkpoints (as a new process would);
`run` drives the same graph in one process, with its hooks."""

import json
from dataclasses import replace
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command

from cofabriq.loop import refine as refine_mod
from cofabriq.loop.engine import AgentRun, Halted
from cofabriq.loop.graph import (
    ALLOWED,
    Deps,
    FlowState,
    GateAnswer,
    build_graph,
    in_memory,
    read_status,
    thread_config,
)
from cofabriq.loop.refine import Spec
from cofabriq.loop.run import CheckResult, LoopResult
from tests.builders import REPRO, issue, spec
from tests.fakes import (
    EditingAgent,
    Reader,
    deps,
    write,
)


def run_graph(d: Deps, state: FlowState, *answers: GateAnswer) -> FlowState:
    """The graph in one process, as `run` drives it: each answer resumes
    the gate it waits at."""
    graph, config = in_memory(d), thread_config("t")
    graph.invoke(state, config)
    for answer in answers:
        graph.invoke(Command(resume=answer.model_dump()), config)
    return FlowState.model_validate(graph.get_state(config).values)


def stored(d: Deps, state: FlowState) -> dict[str, object]:
    return json.loads((d.runs_dir / f"{state.record}.json").read_text())


YES = GateAnswer(approve=True)


def test_a_rejected_test_is_recorded_and_reaches_lessons(tmp_path: Path) -> None:
    d = deps(tmp_path, EditingAgent(write("src/a.test.sh", REPRO)), ["a.ts fixed"])
    no = GateAnswer(approve=False, reason="tests the wrong thing")
    state = run_graph(d, FlowState(issue=issue(3), route="feature"), YES, no)
    assert (state.outcome, state.note) == (
        "test_rejected",
        "test rejected: tests the wrong thing",
    )
    assert stored(d, state)["result"]["status"] == "test_rejected"  # type: ignore[index]
    (line,) = (d.lessons_dir / "acme-app-rejections.jsonl").read_text().splitlines()
    assert json.loads(line)["gate"] == "test"
    assert not any(d.work_dir.iterdir())  # finish removed the worktree


def test_a_red_build_is_recorded_without_a_review(tmp_path: Path) -> None:
    still = write("src/a.ts", "still bug\n")
    d = deps(
        tmp_path, EditingAgent(write("src/a.test.sh", REPRO), still, still, still), []
    )
    state = run_graph(d, FlowState(issue=issue(3), route="bug"), YES)
    assert state.outcome == "escalated" and state.gated is None
    assert set(stored(d, state)) == {"result", "evidence"}  # no gate, no draft


def test_a_given_spec_skips_refine_and_a_planted_repro_goes_to_the_agent(
    tmp_path: Path,
) -> None:
    editor = EditingAgent(write("src/a.ts", "fixed\n"))
    d = deps(tmp_path, editor, [])
    given = refine_mod.refine(issue(3), d.clone, d.reader, "bug")
    reader = Reader(spec("src/a.ts"))
    d = replace(d, reader=reader)
    state = FlowState(
        issue=issue(3),
        route="bug",
        refined=given,
        planted=str(tmp_path / "bug.test.sh"),
    )
    state = run_graph(d, state, YES)
    assert reader.refiner.calls == []  # the spec was given
    assert editor.roles == ["implement"]  # no repro to write: it was handed in
    assert state.result is not None and state.result.planted
    assert state.outcome == "approved"


def test_run_hooks_judge_before_finish_and_keep_the_worktree(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def judge(
        result: LoopResult, worktree: Path
    ) -> tuple[CheckResult | None, CheckResult | None]:
        seen.append((result.status, (worktree / "src/a.ts").read_text()))
        return CheckResult.verdict("judge", False, "planted repro red"), None

    editor = EditingAgent(write("src/a.test.sh", REPRO), write("src/a.ts", "fixed\n"))
    d = deps(tmp_path, editor, [])
    d = replace(d, judge=judge, keep=True, prefix="")
    state = run_graph(d, FlowState(issue=issue(3), route="bug"), YES)
    assert seen == [("green", "fixed\n")]  # on the loop's tree, before finish
    record = stored(d, state)
    assert record["judge"]["ok"] is False and record["valid"] is None  # type: ignore[index]
    assert (d.work_dir / "acme-app-3" / "src/a.ts").read_text() == "fixed\n"


def test_a_halt_during_review_is_recorded_without_a_gate(tmp_path: Path) -> None:
    class Halting(Reader):  # refines, then the budget is spent at review
        def __call__(
            self, prompt: str, schema: dict[str, object], cwd: Path
        ) -> AgentRun:
            if schema == Spec.model_json_schema():
                return super().__call__(prompt, schema, cwd)
            raise Halted("run budget spent")

    editor = EditingAgent(write("src/a.test.sh", REPRO), write("src/a.ts", "fixed\n"))
    d = replace(deps(tmp_path, editor, []), reader=Halting(spec("src/a.ts")))
    state = run_graph(d, FlowState(issue=issue(3), route="bug"), YES)
    assert state.note == "halted before the review was done: run budget spent"
    assert state.outcome == "green" and state.gated is None
    assert "gate" not in stored(d, state) and "draft" not in stored(d, state)


def test_a_halt_in_the_loop_ends_halted_and_is_recorded(tmp_path: Path) -> None:
    def halt(_: Path) -> None:
        raise Halted("stopped")

    d = deps(tmp_path, EditingAgent(write("src/a.test.sh", REPRO), halt), [])
    state = run_graph(d, FlowState(issue=issue(3), route="bug"), YES)
    assert state.outcome == "halted"
    assert stored(d, state)["result"]["status"] == "halted"  # type: ignore[index]


def test_feature_flow_pauses_twice_and_resumes_in_a_new_graph(tmp_path: Path) -> None:
    editor = EditingAgent(write("src/a.test.sh", REPRO), write("src/a.ts", "fixed\n"))
    d = deps(tmp_path, editor, ["a.ts says fixed"])
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED))
    config: RunnableConfig = {"configurable": {"thread_id": "t"}}

    first = build_graph(d).compile(checkpointer=saver)
    first.invoke(FlowState(issue=issue(3), route="feature"), config)
    assert first.get_state(config).next == ("plan_gate",)

    second = build_graph(d).compile(checkpointer=saver)  # "another process"
    second.invoke(Command(resume={"approve": True}), config)
    snap = second.get_state(config)
    assert snap.next == ("test_gate",)
    assert "+grep -q fixed" in snap.tasks[0].interrupts[0].value["diff"]
    assert editor.roles == ["reproduce"]  # the builder hasn't run yet

    third = build_graph(d).compile(checkpointer=saver)
    third.invoke(Command(resume={"approve": True}), config)
    state = FlowState.model_validate(third.get_state(config).values)
    assert state.result is not None and state.result.status == "green"
    assert state.gated is not None and state.gated.status == "approved"
    # recorded like `cofabriq run`: the viewer's Runs shows it, with the PR body
    runs = tmp_path / "runs"
    assert (runs / f"{state.record}.json").is_file()
    assert "a.ts" in (runs / f"{state.record}.pr.md").read_text()
    record = (runs / f"{state.record}.json").read_text()
    assert '"title": "Feature #3:' in record
    stored = json.loads(record)
    assert stored["result"] == state.result.model_dump(mode="json")  # before review
    assert "judge" not in stored  # no answer key: the key stays out
    assert editor.roles == ["reproduce", "implement"]
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").iterdir())


def test_a_rejected_plan_ends_the_flow(tmp_path: Path) -> None:
    d = deps(tmp_path, EditingAgent(), [])
    graph = build_graph(d).compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "t"}}
    graph.invoke(FlowState(issue=issue(3), route="bug"), config)
    paused = read_status(graph, "t")
    assert paused is not None and (paused.waiting, paused.diff) == ("plan", "")
    graph.invoke(Command(resume={"approve": False}), config)
    state = FlowState.model_validate(graph.get_state(config).values)
    assert (state.stage, state.note, state.outcome) == (
        "rejected",
        "plan rejected",
        "rejected",
    )
    assert not graph.get_state(config).next
    assert read_status(graph, "nothing") is None


def test_a_gate_answer_keeps_500_characters_of_its_reason() -> None:
    a = GateAnswer(approve=False, reason=f"  {'x' * 600} ")
    assert a.note("test") == f"test rejected: {'x' * 500}"
    assert GateAnswer(approve=False).note("plan") == "plan rejected"


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


def test_a_spec_that_isnt_ready_goes_back_before_any_gate(tmp_path: Path) -> None:
    d = deps(tmp_path, EditingAgent(), [])  # a feature without criteria
    graph = build_graph(d).compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "t"}}
    graph.invoke(FlowState(issue=issue(3), route="feature"), config)
    state = FlowState.model_validate(graph.get_state(config).values)
    assert state.stage == "failed" and "not ready" in state.note
    assert not graph.get_state(config).next  # no gate waits
