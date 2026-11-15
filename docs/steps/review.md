# Review: a second agent checks the change

A read-only reviewer with its own context reads the spec, the test and the code change, and reports findings; only a finding a test proves can block.

## Why

A reviewer's claim ("this breaks across DST") is cheap to make and hard for a
person to check while skimming a PR. In a timed human round, 2 of 3 wrong PRs
were merged although the reviewer had flagged all three. So a blocking finding
needs a proof: an agent writes a test that quotes a line of the approved spec
and is red on the change. Proven, the builder fixes it and a second review
runs. Unproven, it is listed at the top of the PR, which still opens (ADR-008).

Before any review is paid for, the target's declared invariant (the sentinel)
is checked in code; a violation blocks.

## Code

`cofabriq.loop.gate.review_gate`
