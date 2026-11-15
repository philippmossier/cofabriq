# ADR-010: LangGraph for the outer loop, every gate its own node

## Status

Accepted, 2026-10-18. Amended 2026-11-03: `cofabriq run` drives the same
graph in one process, so the graph is the only orchestrator of a run.
Amended 2026-11-14: durable-execution engines as an alternative.
Amended 2026-11-15: every route stops at the test gate, and a test sent back
is written again: the graph's one loop.

## Context

Until 0.4 a run blocks while it waits for the dev: `cofabriq run` asks "Approve
the plan?" in the terminal and the process waits. The browser gate (0.5) can't
work that way. A run must stop at the plan gate (and, for a feature, at the test
gate), keep everything it has, and continue minutes or hours later, in another
process, when the dev clicks Approve.

The spec had picked LangGraph for this in 0.3 and it slipped: 0.2 was built in
plain Python and worked. So 0.5 started with a half-day spike and criteria set
in advance: under about 300 lines, the evals untouched, resume after a restart,
a failed run debuggable without LangSmith.

The spike's main finding holds for any framework: on resume, the interrupted
node runs again from its start. A gate in the middle of a step would rerun the
step before it, and an agent would write a different test than the one the dev
approved. So every pause point has to be a step boundary.

## Decision

- **The outer loop of `cofabriq run` is a LangGraph graph** (`loop/graph.py`):
  refine, plan gate, setup, write the test, test gate, build, review, record.
  The gates are `interrupt()` calls, each in its own node.
- **`run_loop` is split into stages** (`start_stage`, `test_stage`,
  `build_stage`, `pack` over a JSON `Progress`). The graph's nodes call the
  stages; the evals keep calling `run_loop`, which composes the same stages
  without pausing. No eval runs through the graph.
- **Checkpoints go to Postgres** (the instance cofabriq already runs), keyed by
  `owner/name#number`. The serializer restores only the types on an explicit
  allow-list.
- **What a paused run must keep lives in its state**, the run budget included:
  a resumed run continues with what is left, not a fresh budget.
- **`run` uses the graph too** (amendment): an in-memory checkpointer, each
  interrupt answered in the terminal. What only `run` needs stays out of the
  graph's flow: the answer-key judge on a planted bug and `--keep` are two
  hooks the CLI passes in (the judge runs in `record`, before the worktree is
  removed); `--spec` and `--planted` are inputs in the state; publishing
  (`--apply`) runs after the graph, from the stored record.
- **Every route stops at the test gate** (amendment, 2026-11-15), bugs too,
  not only features. A test can be red for a reason the issue never named
  (on playground #8 it asserted `color-scheme` before the reported colour;
  the build turned it green and the reviewer only noted it), and once the
  build has passed against it, it proves nothing. The gate shows how the
  test fails on the unchanged code. `run --yes` approves it; the evals don't
  go through the graph, so their numbers stay comparable.
- **A test sent back is written again** (amendment): the test gate's third
  answer, next to approve and stop, needs a reason. The graph goes from
  `test_gate` back to `write_test` with the rejected test and the reason in
  the state; the worktree is reset first, the plan stays approved. The bound
  (3 rewrites) is a counter in the state, not a graph construct, so a
  resumed flow keeps it.

## Consequences

- A run can wait at a gate for as long as it takes, and the browser gate is a
  thin layer on top: answer the gate, resume in a background thread.
- Measured in the spike: about 300 lines for graph and CLI, resume live across
  three processes, the state read back into cofabriq's own Pydantic models
  (`cofabriq flow show`), no LangSmith needed.
- 24 more packages (langgraph, langchain-core, langsmith, requests and others).
  LangSmith sends nothing unless `LANGSMITH_TRACING` is set.
- The checkpoint allow-list has to grow with the state; a missing type shows up
  as a warning now and will be an error in a later LangGraph version.
- Two ways to run one issue: `run` (blocking, the terminal gates) and `flow`
  (paused, the browser gate). Until the amendment both shared only the
  stages, and the orchestration around them drifted: only `run` checked
  blockers, only the flow kept rejection reasons, and each stored a different
  result. Now both drive one graph: the same checks, the same record (the
  loop's result before review), and every run that got past setup is
  recorded, a flow's red build or rejected test too.
- One more click per bug run, and a run's path through the graph is no
  longer a straight line: the step view shows a rewritten test as a second
  "write a failing test" step. Every rejection is still kept for `lessons`.

## Alternatives considered

- **Plain Python with a stored state and "resume from step X"**: about 80
  lines, no dependencies. The step-boundary finding applies to it unchanged, so
  the real work (the stages) is the same. Rejected because LangGraph gives
  interrupts, checkpoint history and the Postgres saver ready-made, and it is
  the mainstream tool for paused human gates.
- **Gates inside `run_loop` as callbacks** (what 0.3's test gate did): fine
  while the process waits, impossible once the dev answers from another
  process.
- **A durable-execution engine (Temporal, Inngest)** (amendment): a server
  that re-drives a failed step and memoizes the finished ones, so a crashed
  run continues on its own. LangGraph's checkpoints are not that: the state
  is saved after every node, but nothing resumes a thread unless someone
  invokes it again, the interrupted node restarts from its beginning, and
  retries are opt-in per node (`RetryPolicy`; cofabriq uses none, the
  build's three attempts are its own loop). Rejected for now: a run is one
  dev's issue, a crash is rare and visible (the flow keeps its checkpoint,
  the dev reruns it), and a workflow server is one more service to
  self-host (ADR-009). Revisit when runs go unattended (a batch of issues
  overnight), where a crashed run must restart without a person.
- **A rejected test ends the run** (what 0.5 did, amendment): the dev's
  reason was kept only for `lessons`, and a test off by one case cost a full
  restart, refine included. Rejected: the plan was fine, only the test needed
  another try.
- **Back to refine on a rejected test** (amendment): a new spec for a test
  problem would make the dev approve the plan again. Rejected for the test
  gate; Start again (a new flow that reads why the last one ended) covers a
  plan that turned out wrong.
