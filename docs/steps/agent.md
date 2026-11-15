# Agent session

One Claude Code session in headless mode: a prompt in, a JSON answer out that must match a schema; many model calls and tool uses inside, seen from outside as one step.

## Why

cofabriq delegates reading and editing code to a coding agent instead of
parsing code itself. Each session is contained: no shell (`--restricted`, an
explicit tool list), files only inside the working directory, no MCP servers,
none of the caller's own Claude Code setup, a budget and a turn cap. The
target's own CLAUDE.md is passed in as rules. What the agent writes is only
ever run by cofabriq, in the sandbox.

A kill switch (`cofabriq stop`) stops every session before it starts, and one
run's sessions share one budget.

## Code

`cofabriq.loop.engine.ClaudeCode`
