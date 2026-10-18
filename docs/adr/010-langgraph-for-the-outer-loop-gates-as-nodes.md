# ADR-010: LangGraph for the outer loop, every gate its own node

## Status

Accepted, 2026-10-18.

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
  refine, plan gate, setup, write the test, test gate (features), build, review,
  record. The gates are `interrupt()` calls, each in its own node.
- **`run_loop` is split into stages** (`start_stage`, `test_stage`,
  `build_stage`, `pack` over a JSON `Progress`). The graph's nodes call the
  stages; the evals keep calling `run_loop`, which composes the same stages
  without pausing. No eval runs through the graph.
- **Checkpoints go to Postgres** (the instance cofabriq already runs), keyed by
  `owner/name#number`. The serializer restores only the types on an explicit
  allow-list.
- **What a paused run must keep lives in its state**, the run budget included:
  a resumed run continues with what is left, not a fresh budget.

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
  (paused, the browser gate). Both share the stages.

## Alternatives considered

- **Plain Python with a stored state and "resume from step X"**: about 80
  lines, no dependencies. The step-boundary finding applies to it unchanged, so
  the real work (the stages) is the same. Rejected because LangGraph gives
  interrupts, checkpoint history and the Postgres saver ready-made, and it is
  the mainstream tool for paused human gates.
- **Gates inside `run_loop` as callbacks** (what 0.3's test gate did): fine
  while the process waits, impossible once the dev answers from another
  process.
