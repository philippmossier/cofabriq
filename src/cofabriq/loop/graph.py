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

import json
import re
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel

from cofabriq.issues.model import Issue
from cofabriq.loop import gate as gate_mod
from cofabriq.loop import pr as pr_mod
from cofabriq.loop import refine as refine_mod
from cofabriq.loop.engine import Agent, AgentError, Guard
from cofabriq.loop.intake import not_ready
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
from cofabriq.map import components as components_mod

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
    record: str = ""  # the finished run's file in data/runs (the viewer's Runs)
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
    targets_dir: Path | None = None  # risk tier and components for the PR body
    runs_dir: Path | None = None  # where a finished flow is recorded, as `run` does

    def worktree(self, issue: Issue) -> Path:
        return self.work_dir / f"flow-{issue.repo.replace('/', '-')}-{issue.number}"


def build_graph(deps: Deps) -> StateGraph[FlowState]:
    def refine(s: FlowState) -> dict[str, object]:
        try:
            refined = refine_mod.refine(s.issue, deps.clone, deps.reader, s.route)
        except (AgentError, refine_mod.NoCriteria) as exc:  # back to the dev
            return {"stage": "failed", "note": f"not ready: {exc}"}
        # the readiness check `run` does: back to the dev before any gate
        reasons = not_ready(refined, refine_mod.tracked(deps.clone, refined.commit))
        if reasons:
            note = f"not ready: {'; '.join(reasons)}"
            return {"refined": refined, "stage": "failed", "note": note}
        return {"refined": refined}

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

    def record(s: FlowState) -> dict[str, object]:
        """The finished run in data/runs, the format `cofabriq run` writes
        (result, gate, draft PR body unless blocked), so the viewer shows it."""
        if deps.runs_dir is None or s.gated is None or s.refined is None:
            return {}
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        stem = f"{s.issue.repo.replace('/', '-')}-{s.issue.number}-{stamp}"
        out: dict[str, object] = {
            "result": s.gated.result.model_dump(mode="json"),
            "gate": s.gated.model_dump(mode="json"),
        }
        deps.runs_dir.mkdir(parents=True, exist_ok=True)
        if s.gated.status != "blocked" and deps.targets_dir is not None:
            draft = pr_mod.draft(
                issue_title=s.issue.title,
                spec=s.refined.spec,
                result=s.gated.result,
                tests=deps.rules.tests,
                risk_rules=pr_mod.load_risk(deps.targets_dir, s.issue.repo),
                components=components_mod.load_rules(deps.targets_dir, s.issue.repo),
                review=s.gated.review,
                rounds=s.gated.rounds,
                route=s.route,
            )
            out["draft"] = draft.model_dump(mode="json")
            (deps.runs_dir / f"{stem}.pr.md").write_text(draft.body, "utf-8")
        (deps.runs_dir / f"{stem}.json").write_text(json.dumps(out, indent=2), "utf-8")
        return {"record": stem}

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
        "record": record,
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
    g.add_conditional_edges("refine", go_on, {"next": "plan_gate", "finish": "finish"})
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
    g.add_edge("review", "record")
    g.add_edge("record", "finish")
    g.add_edge("finish", END)
    return g


@contextmanager
def checkpoints(dsn: str) -> Generator[PostgresSaver]:
    """The Postgres checkpointer (tables made on first use), restoring only
    the types in `ALLOWED`."""
    serde = JsonPlusSerializer(allowed_msgpack_modules=ALLOWED)
    with psycopg.Connection[DictRow].connect(
        dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        saver = PostgresSaver(conn, serde=serde)  # pyright: ignore[reportArgumentType]
        saver.setup()
        yield saver


@contextmanager
def compiled(deps: Deps, dsn: str) -> Generator[CompiledStateGraph[FlowState]]:
    """The graph with its Postgres checkpointer."""
    with checkpoints(dsn) as saver:
        yield build_graph(deps).compile(checkpointer=saver)


def thread_id(issue: Issue) -> str:
    return f"{issue.repo}#{issue.number}"


class FlowStatus(BaseModel):
    """Where one flow stands, for the browser gate."""

    thread: str
    state: FlowState
    waiting: Literal["plan", "test"] | None  # the gate it waits at
    diff: str = ""  # the frozen test, at the test gate
    running: bool = False  # an answer is being worked off right now
    error: str = ""  # the last answer's run failed


_THREAD = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+$")

type DepsFor = Callable[[str, float], Deps]  # (repo, spent so far) -> deps


class FlowRunner:
    """Answers a flow's gate from the browser: the resume runs in a
    background thread (setup, test, build and review take minutes), one at a
    time per flow; the page polls `status`."""

    def __init__(self, dsn: str, deps_for: DepsFor) -> None:
        self.dsn = dsn
        self.deps_for = deps_for
        self._running: set[str] = set()
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def threads(self) -> list[str]:
        with checkpoints(self.dsn) as saver:
            seen = (
                c.config.get("configurable", {}).get("thread_id")
                for c in saver.list(None)
            )
            # only cofabriq's own threads (`owner/name#n`); others are skipped
            return list(
                dict.fromkeys(str(t) for t in seen if t and _THREAD.match(str(t)))
            )

    def status(self, thread: str) -> FlowStatus | None:
        repo = thread.split("#", 1)[0]
        with compiled(self.deps_for(repo, 0.0), self.dsn) as graph:
            snap = graph.get_state(_config(thread))
        if not snap.values:
            return None
        payloads = [i.value for t in snap.tasks for i in t.interrupts]
        gate = payloads[0] if payloads else None
        with self._lock:
            running, error = thread in self._running, self._errors.get(thread, "")
        return FlowStatus(
            thread=thread,
            state=FlowState.model_validate(snap.values),
            waiting=gate["gate"] if gate else None,
            diff=gate.get("diff", "") if gate else "",
            running=running,
            error=error,
        )

    def answer(self, thread: str, approve: bool) -> bool:
        """Start working off the answer; False if the flow waits at no gate
        or an answer is already running."""
        current = self.status(thread)
        if current is None or current.waiting is None or current.running:
            return False
        with self._lock:
            if thread in self._running:
                return False
            self._running.add(thread)
            self._errors.pop(thread, None)
        spent = current.state.spent_usd
        worker = threading.Thread(
            target=self._resume, args=(thread, approve, spent), daemon=True
        )
        worker.start()
        return True

    def _resume(self, thread: str, approve: bool, spent: float) -> None:
        repo = thread.split("#", 1)[0]
        try:
            with compiled(self.deps_for(repo, spent), self.dsn) as graph:
                graph.invoke(Command(resume=approve), _config(thread))
        except Exception as exc:  # shown on the page; the checkpoint stays
            with self._lock:
                self._errors[thread] = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._running.discard(thread)


def _config(thread: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread}}
