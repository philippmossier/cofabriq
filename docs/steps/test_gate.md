# Test gate: your approval of the test

Features only: you read the acceptance test before any code exists; once approved, it is frozen.

## Why

For a feature, the test is the specification in executable form. If it tests
the wrong thing, a green build is worthless. Approving it here, and freezing it
(the builder may not edit it), means the code is judged against what you agreed
to, not against a test the builder adjusted.

## Code

`cofabriq.loop.graph.build_graph`
