# Build: fix the code against the test

An agent changes the code until the red test is green and the target's own checks pass, at most three attempts.

## Why

The test from the previous step is the judge, not the agent's own claim. After
each attempt cofabriq runs the test and the target's checks (types, lint, the
whole test suite) in the sandbox; a failure goes back to the agent with the
output. Three attempts, then the run ends as `escalated`: a person looks at it.

Edits to the frozen test are reverted, so the builder can't make the test
easier instead of the code right.

## Code

`cofabriq.loop.run.build_stage`
