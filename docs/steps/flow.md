# Run the flow

One process's part of a run: `flow start` up to the first gate, then one more each time a gate is answered.

## Why

A run waits for a person at its gates, sometimes for hours, so it can't be one
long process. LangGraph saves the run's state after every step (a checkpoint in
Postgres), the process ends at the gate, and the answer resumes it in a new
process (`serve`, or `flow resume`). Each process is its own trace; a resumed
one links back to the first, the way an HTTP call carries a `traceparent`
header (ADR-010, ADR-011).

Alternative: a durable-execution engine (Temporal, Inngest) would also restart
a run that crashed, at the price of a workflow server to run. Not needed while
a person starts every run.

## Code

`cofabriq.loop.graph.invoke`
