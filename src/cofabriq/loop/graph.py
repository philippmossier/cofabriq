"""The core loop for one issue as a LangGraph graph whose human gates are
interrupts. It is the one orchestrator: `cofabriq run` drives it in one
process (an in-memory checkpointer, the gates answered in the terminal);
`flow start/resume` and the browser gate (`serve --write`) checkpoint it
in Postgres, so a run can wait for the dev for hours and resume in
another process.

    refine -> plan_gate -> setup -> write_test -> test_gate -> build -> review
                                                  (feature only)
      -> record -> finish

    not ready, plan rejected                 -> finish
    any other end once setup ran (setup
    failed, no repro, test rejected, halted,
    a red build)                             -> record -> finish

Each gate is its own node on purpose: on resume LangGraph re-runs the
interrupted node from its start, so anything expensive before an
`interrupt()` would run twice (and an agent would write a different test).
The nodes are `run_loop`'s own stages (`start_stage`, `test_stage`,
`build_stage`, `pack`) and `review_gate`; the evals keep calling
`run_loop`, which composes the same stages without pausing.

The worktree lives on disk between nodes and is removed by `finish`
(unless `Deps.keep`). State is a Pydantic model; the checkpoint
serializer only restores the types listed in `ALLOWED` (LangGraph's
msgpack allow-list), so a checkpoint can't smuggle in another class.

Each node is a span (`tracing.py`); `invoke` is the root of one process's
part of a run, linked to the trace the flow started in.
"""

import re
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel, field_validator

from cofabriq import tracing
from cofabriq.files import stamp
from cofabriq.git import files_at
from cofabriq.issues.model import Issue
from cofabriq.loop import gate as gate_mod
from cofabriq.loop import refine as refine_mod
from cofabriq.loop.engine import Agent, AgentError, Guard, Halted
from cofabriq.loop.intake import not_ready
from cofabriq.loop.lessons import log_rejection
from cofabriq.loop.record import RunRecord, draft_for, sentinel_for, write_record
from cofabriq.loop.run import (
    FEATURE,
    INSTRUCTIONS,
    Attempt,
    CheckResult,
    LoopResult,
    LoopRules,
    Progress,
    Status,
    build_stage,
    collect_evidence,
    pack,
    start_stage,
    test_stage,
)
from cofabriq.loop.sandbox import Sandbox
from cofabriq.loop.worktree import diff, remove_worktree
from cofabriq.targets import repo_slug

type Node = Callable[["FlowState"], dict[str, object]]
type Stage = Literal[
    "refined", "approved", "rejected", "set_up", "tested", "built", "reviewed", "failed"
]


class FlowState(BaseModel):
    """Everything a paused run needs to go on in another process."""

    issue: Issue
    route: refine_mod.Route
    stage: Stage = "refined"
    note: str = ""  # why it stopped (not ready, rejected, halted)
    refined: refine_mod.Refined | None = None  # given: no refine run (`--spec`)
    planted: str = ""  # a planted repro handed to the agent (`run --planted`)
    progress: Progress | None = None  # the loop between its stages
    spent_usd: float = 0.0  # all agents so far: a resumed run keeps its budget
    record: str = ""  # the finished run's file in data/runs (the viewer's Runs)
    result: LoopResult | None = None  # the loop's, before the review gate
    gated: gate_mod.Gated | None = None
    trace: str = ""  # the first trace's `traceparent`: each resume links to it
    earlier: str = ""  # why the last attempt on this issue ended (refine reads it)

    @property
    def outcome(self) -> str:
        """How it ended: the review gate's status, else the loop's, else the
        stage it stopped at."""
        if self.gated is not None:
            return self.gated.status
        return self.result.status if self.result is not None else self.stage

    @property
    def lesson(self) -> str:
        """What a new attempt should know: why this one stopped short (a
        rejected plan's reason, a spec that wasn't ready); "" after a fix
        or with no reason given."""
        return "" if self.outcome in ("approved", "green") else self.note


class GateAnswer(BaseModel):
    """The dev's answer at a gate. It resumes the graph as a plain dict
    (`model_dump`): the checkpoint keeps it, and `ALLOWED` stays as it is."""

    approve: bool
    reason: str = ""  # why, when rejecting; kept for `cofabriq lessons`

    @field_validator("reason")
    @classmethod
    def _trim(cls, reason: str) -> str:
        return reason.strip()[:500]

    def note(self, gate: str) -> str:
        return f"{gate} rejected{f': {self.reason}' if self.reason else ''}"


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


# the answer key on the tree the loop left (`run` on a planted bug): the
# planted repro's verdict and the agent's repro's validity; may reset the code
type Judge = Callable[[LoopResult, Path], tuple[CheckResult | None, CheckResult | None]]


@dataclass(frozen=True)
class Deps:
    """What the nodes need that isn't state (not checkpointed)."""

    clone: Path
    work_dir: Path
    rules: LoopRules
    reader: Agent
    editor: Agent
    sandbox: Sandbox
    targets_dir: Path  # risk tier and components for the PR body
    runs_dir: Path  # where a finished run is recorded
    lessons_dir: Path  # rejections with their reason, for `lessons`
    guard: Guard | None = None  # the run budget both agents count against
    # run-only hooks (`flow` and `serve --write` pass none)
    judge: Judge | None = None  # in `record`, after the evidence
    keep: bool = False  # `finish` leaves the worktree (`run --keep`)
    # a run and a paused flow on the same issue never share a worktree
    prefix: str = "flow-"

    def worktree(self, issue: Issue) -> Path:
        name = f"{self.prefix}{repo_slug(issue.repo)}-{issue.number}"
        return self.work_dir / name


def build_graph(deps: Deps) -> StateGraph[FlowState]:
    def stopped(
        s: FlowState,
        p: Progress,
        status: Status,
        stage: Stage = "failed",
        note: str = "",
    ) -> dict[str, object]:
        """An early end after setup, packed as `run_loop` packs it, so it
        is recorded too."""
        result = pack(p, deps.worktree(s.issue), status, [])
        return {"progress": p, "stage": stage, "note": note, "result": result}

    def refine(s: FlowState) -> dict[str, object]:
        refined = s.refined
        try:
            if refined is None:
                refined = refine_mod.refine(
                    s.issue, deps.clone, deps.reader, s.route, s.earlier
                )
        except (AgentError, refine_mod.NoCriteria) as exc:  # back to the dev
            return {"stage": "failed", "note": f"not ready: {exc}"}
        except Halted as exc:
            return {"stage": "failed", "note": f"halted: {exc}"}
        # the readiness check: back to the dev before any gate
        reasons = not_ready(refined, set(files_at(deps.clone, refined.commit)))
        if reasons:
            note = f"not ready: {'; '.join(reasons)}"
            return {"refined": refined, "stage": "failed", "note": note}
        return {"refined": refined}

    def plan_gate(s: FlowState) -> dict[str, object]:
        assert s.refined is not None
        a = GateAnswer.model_validate(
            interrupt({"gate": "plan", "spec": s.refined.spec.model_dump()})
        )
        tracing.answered(a.approve)
        if not a.approve:
            log_rejection(deps.lessons_dir, s.issue, s.route, "plan", a.reason)
        return {
            "stage": "approved" if a.approve else "rejected",
            "note": "" if a.approve else a.note("plan"),
        }

    def setup(s: FlowState) -> dict[str, object]:
        assert s.refined is not None
        p = start_stage(
            repo=s.issue.repo,
            number=s.issue.number,
            commit=s.refined.commit,
            clone=deps.clone,
            worktree=deps.worktree(s.issue),
            repro_file=Path(s.planted) if s.planted else None,
            rules=deps.rules,
            sandbox=deps.sandbox,
        )
        if p.status is not None:
            return stopped(s, p, p.status)
        return {"progress": p, "stage": "set_up"}

    def write_test(s: FlowState) -> dict[str, object]:
        assert s.refined is not None and s.progress is not None
        p = s.progress.model_copy(deep=True)
        try:
            p = test_stage(
                p,
                spec=s.refined.spec,
                rules=deps.rules,
                worktree=deps.worktree(s.issue),
                agent=deps.editor,
                sandbox=deps.sandbox,
                route=s.route,
            )
        except Halted:  # the kill switch: what was done so far, no more agents
            p.red, p.status = None, "halted"
        if p.status is not None:
            return stopped(s, p, p.status)
        return {"progress": p, "stage": "tested"}

    def test_gate(s: FlowState) -> dict[str, object]:
        assert s.progress is not None
        test = diff(deps.worktree(s.issue), [])
        a = GateAnswer.model_validate(interrupt({"gate": "test", "diff": test}))
        tracing.answered(a.approve)
        if a.approve:
            return {}
        log_rejection(deps.lessons_dir, s.issue, s.route, "test", a.reason)
        return stopped(s, s.progress, "test_rejected", "rejected", a.note("test"))

    def build(s: FlowState) -> dict[str, object]:
        assert s.refined is not None and s.progress is not None
        worktree = deps.worktree(s.issue)
        try:
            attempts = build_stage(
                s.progress,
                spec=s.refined.spec,
                rules=deps.rules,
                worktree=worktree,
                agent=deps.editor,
                sandbox=deps.sandbox,
                instructions=FEATURE if s.route == "feature" else INSTRUCTIONS,
            )
        except Halted:
            p = s.progress.model_copy(update={"red": None})
            return {"stage": "built", "result": pack(p, worktree, "halted", [])}
        status: Status = "green" if attempts[-1].green else "escalated"
        return {
            "stage": "built",
            "result": pack(s.progress, worktree, status, attempts),
        }

    def review(s: FlowState) -> dict[str, object]:
        assert s.refined is not None and s.result is not None
        try:
            gated = gate_mod.review_gate(
                spec=s.refined.spec,
                rules=deps.rules,
                result=s.result,
                worktree=deps.worktree(s.issue),
                reader=deps.reader,
                editor=deps.editor,
                sandbox=deps.sandbox,
                sentinel=sentinel_for(
                    deps.targets_dir,
                    s.issue.repo,
                    deps.clone,
                    s.refined.commit,
                    deps.rules.tests,
                ),
            )
        except Halted as exc:  # recorded without a gate, so no PR body either
            note = f"halted before the review was done: {exc}"
            return {"stage": "reviewed", "note": note}
        return {"stage": "reviewed", "gated": gated}

    def record(s: FlowState) -> dict[str, object]:
        """The run in data/runs: the loop's result (before review), the
        gate, the judge's verdicts (a hook), the draft PR body unless
        blocked or not reviewed."""
        assert s.refined is not None and s.result is not None
        stem = f"{repo_slug(s.issue.repo)}-{s.issue.number}-{stamp()}"
        worktree = deps.worktree(s.issue)
        blocked = s.gated is not None and s.gated.status == "blocked"
        final = s.gated.result if s.gated is not None else s.result
        evidence: list[str] = []
        if s.result.status == "green" and not blocked:  # the final change's
            evidence = collect_evidence(
                deps.rules, worktree, final.repro, deps.sandbox, deps.runs_dir / stem
            )
        out = RunRecord(
            result=s.result, evidence=evidence, trace=tracing.run_id(s.trace)
        )
        if deps.judge is not None:  # last on the tree: it may reset the code
            out.judge, out.valid = deps.judge(s.result, worktree)
        if s.gated is not None:
            out.gate = s.gated
        if s.gated is not None and not blocked:
            out.draft = draft_for(
                deps.targets_dir,
                s.issue.repo,
                title=s.issue.title,
                spec=s.refined.spec,
                result=s.gated.result,
                tests=deps.rules.tests,
                review=s.gated.review,
                rounds=s.gated.rounds,
                route=s.route,
                evidence=len(evidence),
            )
        write_record(deps.runs_dir, stem, out)
        return {"record": stem}

    def finish(s: FlowState) -> dict[str, object]:
        if not deps.keep:
            remove_worktree(deps.clone, deps.worktree(s.issue))
        return {}

    def go_on(s: FlowState) -> str:
        """On, or the end: recorded once the loop has a result."""
        if s.stage not in ("rejected", "failed"):
            return "next"
        return "record" if s.result is not None else "finish"

    def after_test(s: FlowState) -> str:
        return s.route if go_on(s) == "next" else go_on(s)

    def after_build(s: FlowState) -> str:
        return "review" if s.result and s.result.status == "green" else "record"

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

    # the function that does a node's work, for the viewer's code link; the
    # gates and the tail are their own node functions
    work: dict[str, Callable[..., object]] = {
        "refine": refine_mod.refine,
        "setup": start_stage,
        "write_test": test_stage,
        "build": build_stage,
        "review": gate_mod.review_gate,
    }

    def counted(name: str, node: Node) -> Node:
        """The node in its own span, with the run's spending so far and the
        first trace written into the state; what it changed in the state is
        kept for the viewer."""

        def run(s: FlowState) -> dict[str, object]:
            code = work.get(name, node)
            span_of = tracing.span(
                f"node {name}", expected=(GraphInterrupt,), code=code
            )
            with span_of as span:
                try:
                    update = node(s)
                except GraphInterrupt:  # a gate: the process stops here
                    span.set_attribute("cofabriq.paused", True)
                    raise
                tracing.capture(span, update=update)
                if not s.trace:
                    update["trace"] = tracing.traceparent()
            if deps.guard is not None:
                update["spent_usd"] = deps.guard.spent_usd
            return update

        return run

    nodes = {name: counted(name, node) for name, node in nodes.items()}
    for name, node in nodes.items():
        g.add_node(name, node)  # pyright: ignore[reportArgumentType,reportUnknownMemberType]
    ends = {"finish": "finish", "record": "record"}
    g.add_edge(START, "refine")
    g.add_conditional_edges("refine", go_on, {"next": "plan_gate", **ends})
    g.add_conditional_edges("plan_gate", go_on, {"next": "setup", **ends})
    g.add_conditional_edges("setup", go_on, {"next": "write_test", **ends})
    g.add_conditional_edges(
        "write_test", after_test, {"feature": "test_gate", "bug": "build", **ends}
    )
    g.add_conditional_edges("test_gate", go_on, {"next": "build", **ends})
    g.add_conditional_edges("build", after_build, {"review": "review", **ends})
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


def in_memory(deps: Deps) -> CompiledStateGraph[FlowState]:
    """The graph for one process (`cofabriq run`): its checkpoints in
    memory, under the same allow-list."""
    saver = InMemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED))
    return build_graph(deps).compile(checkpointer=saver)


def thread_id(issue: Issue) -> str:
    return f"{issue.repo}#{issue.number}"


def thread_config(thread: str) -> RunnableConfig:
    """The config that points a graph call at one flow's checkpoints."""
    return {"configurable": {"thread_id": thread}}


def invoke(
    graph: CompiledStateGraph[FlowState],
    given: FlowState | GateAnswer,
    thread: str,
    on_trace: Callable[[str], None] | None = None,
) -> None:
    """Start a flow, or resume it with the dev's answer, to its next gate or
    its end: one trace per call, a resume linked to the flow's first.
    `on_trace` gets the call's traceparent before any node runs."""
    config = thread_config(thread)
    resume = isinstance(given, GateAnswer)
    first = str(graph.get_state(config).values.get("trace", "")) if resume else ""
    attributes = {"cofabriq.thread": thread, "cofabriq.resume": resume}
    with tracing.span(tracing.FLOW, attributes, link=first, code=invoke):
        if on_trace is not None:
            on_trace(tracing.traceparent())
        if isinstance(given, GateAnswer):
            graph.invoke(Command(resume=given.model_dump()), config)
        else:
            graph.invoke(given, config)


class FlowStatus(BaseModel):
    """Where one flow stands, for the browser gate."""

    thread: str
    state: FlowState
    waiting: Literal["plan", "test"] | None  # the gate it waits at
    diff: str = ""  # the frozen test, at the test gate
    running: bool = False  # an answer is being worked off right now
    error: str = ""  # the last answer's run failed


def read_status(graph: CompiledStateGraph[FlowState], thread: str) -> FlowStatus | None:
    """Where a flow stands, from its newest checkpoint; None if it has none.
    A paused flow's interrupt payload is `{"gate": .., "spec" | "diff": ..}`."""
    snap = graph.get_state(thread_config(thread))
    if not snap.values:
        return None
    payloads = [i.value for t in snap.tasks for i in t.interrupts]
    gate = payloads[0] if payloads else None
    return FlowStatus(
        thread=thread,
        state=FlowState.model_validate(snap.values),
        waiting=gate["gate"] if gate else None,
        diff=gate.get("diff", "") if gate else "",
    )


_THREAD = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+$")

type DepsFor = Callable[[str, float], Deps]  # (repo, spent so far) -> deps
type Prepare = Callable[[str, int], Issue]  # (repo, number) -> the issue, or Refused


class Refused(Exception):
    """A flow that can't start: the message says why (a blocker, no such
    issue, a flow already there). The CLI prints it, the API answers 409."""


def _no_start(repo: str, number: int) -> Issue:
    raise Refused("this server starts no flows")


class FlowRunner:
    """Starts a flow and answers its gates from the browser. The work runs
    in a background thread (refine takes seconds, setup to review minutes),
    one at a time per flow; the page polls `status`. `prepare` runs first,
    in the request: what it refuses comes back at once."""

    def __init__(
        self, dsn: str, deps_for: DepsFor, prepare: Prepare = _no_start
    ) -> None:
        self.dsn = dsn
        self.deps_for = deps_for
        self.prepare = prepare
        self._running: set[str] = set()
        self._errors: dict[str, str] = {}
        # started, no checkpoint yet: the flow page shows it from the start
        self._starting: dict[str, FlowState] = {}
        # a start's traceparent: the state has it only once refine ends, the
        # step view needs it while refine runs
        self._traces: dict[str, str] = {}
        self._lock = threading.Lock()

    def threads(self) -> list[str]:
        with checkpoints(self.dsn) as saver:
            seen = (
                c.config.get("configurable", {}).get("thread_id")
                for c in saver.list(None)
            )
            # only cofabriq's own threads (`owner/name#n`); others are skipped
            stored = [str(t) for t in seen if t and _THREAD.match(str(t))]
        with self._lock:
            return list(dict.fromkeys([*self._starting, *stored]))

    def status(self, thread: str) -> FlowStatus | None:
        repo = thread.split("#", 1)[0]
        with compiled(self.deps_for(repo, 0.0), self.dsn) as graph:
            found = read_status(graph, thread)
        with self._lock:
            if found is None:
                state = self._starting.get(thread)
                if state is None:
                    return None
                found = FlowStatus(thread=thread, state=state, waiting=None)
            else:
                self._starting.pop(thread, None)  # its checkpoint says it now
            if not found.state.trace and thread in self._traces:
                trace = self._traces[thread]
                found.state = found.state.model_copy(update={"trace": trace})
            found.running = thread in self._running
            found.error = self._errors.get(thread, "")
        return found

    def start(
        self, repo: str, number: int, route: refine_mod.Route, again: bool = False
    ) -> str:
        """Prepare the issue, then refine in the background up to the plan
        gate; the flow's thread. `again`: an ended flow on the issue is
        replaced (its checkpoints deleted; its trace, record and lessons stay
        in data/), and why it ended goes to refine. Refused: a flow is there
        (without `again`) or still going, or `prepare` said no."""
        thread = f"{repo}#{number}"
        current = self.status(thread)
        if current is not None and not again:
            raise Refused(f"#{number} has a flow already: open it")
        if current is not None and (current.running or current.waiting):
            raise Refused(f"#{number} is still going: answer or reject its gate first")
        issue = self.prepare(repo, number)
        with self._lock:
            if thread in self._running:
                raise Refused(f"#{number} is starting already")
            self._running.add(thread)
            self._errors.pop(thread, None)
            self._traces.pop(thread, None)
            earlier = current.state.lesson if current is not None else ""
            state = FlowState(issue=issue, route=route, earlier=earlier)
            self._starting[thread] = state
        if current is not None:  # claimed above: no other start can race this
            with checkpoints(self.dsn) as saver:
                saver.delete_thread(thread)
        worker = threading.Thread(
            target=self._work, args=(thread, state, 0.0), daemon=True
        )
        worker.start()
        return thread

    def answer(self, thread: str, answer: GateAnswer) -> bool:
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
            target=self._work, args=(thread, answer, spent), daemon=True
        )
        worker.start()
        return True

    def _work(self, thread: str, given: FlowState | GateAnswer, spent: float) -> None:
        """A start or a resume, to the next gate or the end."""
        repo = thread.split("#", 1)[0]

        def keep(traceparent: str) -> None:
            if isinstance(given, FlowState):  # a resume's state has its trace
                with self._lock:
                    self._traces[thread] = traceparent

        try:
            with compiled(self.deps_for(repo, spent), self.dsn) as graph:
                invoke(graph, given, thread, on_trace=keep)
        except Exception as exc:  # shown on the page; the checkpoint stays
            with self._lock:
                self._errors[thread] = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._running.discard(thread)
