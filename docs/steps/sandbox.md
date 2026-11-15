# Sandbox command

One of the target's commands (install, the test, type check, lint) in a fresh podman container that sees only the worktree.

## Why

The commands run code an agent wrote, and the agent was steered by issue text
anyone can write. So nothing runs on the host: a throwaway container, the
worktree mounted at `/app`, and no network except for the install (ADR-007).

A red result is not an error of this step: while writing the test, red is
exactly what the run wants.

## Code

`cofabriq.loop.sandbox.Podman`
