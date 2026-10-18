"""SPIKE (0.5 step 0): `cofabriq run` as a LangGraph graph whose human gates
are interrupts, checkpointed in Postgres, so a run can wait for the dev for
hours and resume in another process (the browser gate's need).

    refine -> plan_gate -> setup -> write_test -> test_gate -> build -> review -> END
                 |                                  |   (feature only)
                 +-- rejected --> END               +-- rejected --> END

Each gate is its own node on purpose: on resume LangGraph re-runs the
interrupted node from its start, so anything expensive before an
`interrupt()` would run twice (and an agent would write a different test).
The nodes are `run_loop`'s own stages (`start_stage`, `test_stage`,
`build_stage`, `pack`) and `review_gate`; the evals keep calling
`run_loop`, which composes the same stages without pausing.

The worktree lives on disk between nodes and is removed at the end (or by
`finish` after a rejection). State is a Pydantic model; the checkpoint
serializer only restores the types listed in `ALLOWED` (LangGraph's
msgpack allow-list), so a checkpoint can't smuggle in another class.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel

from cofabriq.issues.model import Issue
from cofabriq.loop import gate as gate_mod
from cofabriq.loop import refine as refine_mod
from cofabriq.loop.engine import Agent, Guard
from cofabriq.loop.run import (
    FEATURE,
    INSTRUCTIONS,
    Attempt,
    CheckResult,
    LoopResult,
    Progress,
    Rules,
    Status,
    build_stage,
    diff,
    pack,
    remove_worktree,
    start_stage,
    test_stage,
)
from cofabriq.loop.sandbox import Sandbox

type Node = Callable[["FlowState"], dict[str, object]]
type Stage = Literal[
    "refined", "approved", "rejected", "set_up", "tested", "built", "reviewed", "failed"
]


class FlowState(BaseModel):
    """Everything a paused run needs to go on in another process."""

    issue: Issue
    route: refine_mod.Route
    stage: Stage = "refined"
    note: str = ""  # why it stopped (setup failed, no repro, rejected)
    refined: refine_mod.Refined | None = None
    progress: Progress | None = None  # the loop between its stages
    spent_usd: float = 0.0  # all agents so far: a resumed run keeps its budget
    result: LoopResult | None = None
    gated: gate_mod.Gated | None = None


# the classes a checkpoint may contain (module, class), nothing else
ALLOWED = [
    (cls.__module__, cls.__name__)
    for cls in (
        FlowState,
        Progress,
        Issue,
        refine_mod.Refined,
        refine_mod.Spec,
        refine_mod.FileChange,
        Attempt,
        CheckResult,
        LoopResult,
        gate_mod.Gated,
        gate_mod.Round,
        gate_mod.Proof,
    )
] + [
    ("cofabriq.issues.model", "Comment"),
    ("cofabriq.issues.model", "IssueRef"),
    ("cofabriq.loop.engine", "AgentRun"),
    ("cofabriq.loop.review", "Review"),
    ("cofabriq.loop.review", "Reviewed"),
    ("cofabriq.loop.review", "Finding"),
]


@dataclass(frozen=True)
class Deps:
    """What the nodes need that isn't state (not checkpointed)."""

    clone: Path
    work_dir: Path
    rules: Rules
    reader: Agent
    editor: Agent
    sandbox: Sandbox
    guard: Guard | None = None  # the run budget both agents count against

    def worktree(self, issue: Issue) -> Path:
        return self.work_dir / f"flow-{issue.repo.replace('/', '-')}-{issue.number}"


def build_graph(deps: Deps) -> StateGraph[FlowState]:
    def refine(s: FlowState) -> dict[str, object]:
        return {"refined": refine_mod.refine(s.issue, deps.clone, deps.reader, s.route)}

    def plan_gate(s: FlowState) -> dict[str, object]:
        assert s.refined is not None
        ok = interrupt({"gate": "plan", "spec": s.refined.spec.model_dump()})
        return {
            "stage": "approved" if ok else "rejected",
            "note": "" if ok else "plan rejected",
        }

    def setup(s: FlowState) -> dict[str, object]:
        assert s.refined is not None
        p = start_stage(
            repo=s.issue.repo,
            number=s.issue.number,
            commit=s.refined.commit,
            clone=deps.clone,
            worktree=deps.worktree(s.issue),
            repro_file=None,
            rules=deps.rules,
            sandbox=deps.sandbox,
        )
        failed = p.status is not None
        return {"progress": p, "stage": "failed" if failed else "set_up"}

    def write_test(s: FlowState) -> dict[str, object]:
        assert s.refined is not None and s.progress is not None
        p = test_stage(
            s.progress.model_copy(deep=True),
            spec=s.refined.spec,
            rules=deps.rules,
            worktree=deps.worktree(s.issue),
            agent=deps.editor,
            sandbox=deps.sandbox,
            route=s.route,
        )
        failed = p.status is not None
        return {"progress": p, "stage": "failed" if failed else "tested"}

    def test_gate(s: FlowState) -> dict[str, object]:
        ok = interrupt({"gate": "test", "diff": diff(deps.worktree(s.issue), [])})
        return {} if ok else {"stage": "rejected", "note": "test rejected"}

    def build(s: FlowState) -> dict[str, object]:
        assert s.refined is not None and s.progress is not None
        worktree = deps.worktree(s.issue)
        attempts = build_stage(
            s.progress,
            spec=s.refined.spec,
            rules=deps.rules,
            worktree=worktree,
            agent=deps.editor,
            sandbox=deps.sandbox,
            instructions=FEATURE if s.route == "feature" else INSTRUCTIONS,
        )
        status: Status = "green" if attempts[-1].green else "escalated"
        return {
            "stage": "built",
            "result": pack(s.progress, worktree, status, attempts),
        }

    def review(s: FlowState) -> dict[str, object]:
        assert s.refined is not None and s.result is not None
        gated = gate_mod.review_gate(
            spec=s.refined.spec,
            rules=deps.rules,
            result=s.result,
            worktree=deps.worktree(s.issue),
            reader=deps.reader,
            editor=deps.editor,
            sandbox=deps.sandbox,
        )
        return {"stage": "reviewed", "gated": gated}

    def finish(s: FlowState) -> dict[str, object]:
        remove_worktree(deps.clone, deps.worktree(s.issue))
        return {}

    def go_on(s: FlowState) -> str:
        return "finish" if s.stage in ("rejected", "failed") else "next"

    def after_test(s: FlowState) -> str:
        return "finish" if go_on(s) == "finish" else s.route

    def after_build(s: FlowState) -> str:
        return "review" if s.result and s.result.status == "green" else "finish"

    g = StateGraph(FlowState)
    nodes = {
        "refine": refine,
        "plan_gate": plan_gate,
        "setup": setup,
        "write_test": write_test,
        "test_gate": test_gate,
        "build": build,
        "review": review,
        "finish": finish,
    }

    def counted(node: Node) -> Node:
        """The node, with the run's spending so far written into the state."""

        def run(s: FlowState) -> dict[str, object]:
            update = node(s)
            if deps.guard is not None:
                update["spent_usd"] = deps.guard.spent_usd
            return update

        return run

    nodes = {name: counted(node) for name, node in nodes.items()}
    for name, node in nodes.items():
        g.add_node(name, node)  # pyright: ignore[reportArgumentType,reportUnknownMemberType]
    g.add_edge(START, "refine")
    g.add_edge("refine", "plan_gate")
    g.add_conditional_edges("plan_gate", go_on, {"next": "setup", "finish": "finish"})
    g.add_conditional_edges("setup", go_on, {"next": "write_test", "finish": "finish"})
    g.add_conditional_edges(
        "write_test",
        after_test,
        {"finish": "finish", "feature": "test_gate", "bug": "build"},
    )
    g.add_conditional_edges("test_gate", go_on, {"next": "build", "finish": "finish"})
    g.add_conditional_edges(
        "build", after_build, {"review": "review", "finish": "finish"}
    )
    g.add_edge("review", "finish")
    g.add_edge("finish", END)
    return g


@contextmanager
def compiled(deps: Deps, dsn: str) -> Generator[CompiledStateGraph[FlowState]]:
    """The graph with its Postgres checkpointer (tables made on first use)."""
    serde = JsonPlusSerializer(allowed_msgpack_modules=ALLOWED)
    with psycopg.Connection[DictRow].connect(
        dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        saver = PostgresSaver(conn, serde=serde)  # pyright: ignore[reportArgumentType]
        saver.setup()
        yield build_graph(deps).compile(checkpointer=saver)


def thread_id(issue: Issue) -> str:
    return f"{issue.repo}#{issue.number}"
