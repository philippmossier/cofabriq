# Write a failing test

An agent writes a test from the spec (test files only); cofabriq runs it and it must fail on the unchanged code.

## Why

A fix without a failing test first proves nothing: a test that passes before
the change doesn't show the bug was there. So red is the goal of this step, and
a test that stays green ends the run (`no_repro`). The target's type check and
lint must still pass, so the test itself is valid code.

For a feature, the test is the acceptance test, written from the spec's
criteria, and it stops at the test gate before any code is built.

The agent can edit but has no shell: cofabriq runs every command, in the
sandbox.

## Code

`cofabriq.loop.run.test_stage`
