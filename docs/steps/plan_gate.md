# Plan gate: your approval

The run stops and waits until you approve or reject the spec; a rejection's reason is kept for `cofabriq lessons`.

## Why

The cheapest place to stop a wrong change is before it exists. Approving a spec
with open questions means "go with the plan as it stands": the approval is the
answer to them.

The gate is its own step on purpose: when the run resumes, LangGraph runs the
interrupted step again from its start. Anything expensive before the pause (an
agent) would run twice and could come out different (ADR-010).

## Code

`cofabriq.loop.graph.build_graph`
