# Setup: a clean copy and an install

A throwaway git worktree of the target at the spec's commit, and the target's own install in a container.

## Why

Every run starts from the same tree the spec was written against, so a red
test means the bug, not leftovers. The install is the only command with
network, and it runs before the agent has changed anything: the target's own
lockfile, as a developer would install it. From here on, everything runs with
`--network none` (ADR-007).

## Code

`cofabriq.loop.run.start_stage`
