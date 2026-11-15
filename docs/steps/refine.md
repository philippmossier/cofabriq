# Refine: read the code, write a spec

A read-only agent reads the issue and the target's code and answers with a spec: the problem, the files a fix changes, the tests, a plan, its assumptions and open questions.

## Why

Nothing is written before a person agrees what will be written. The spec is the
contract for every later step: the test writer works from it, the reviewer
checks the change against it, and a finding only blocks when a test quotes it.

The agent gets Read, Grep and Glob only, on a clone checked out at a known
commit. The paths it names are checked against that commit's files afterwards;
an unknown path is flagged, not dropped, since a fix may add a file. A spec
that isn't ready (no criteria for a feature, size L) goes back before any gate.

cofabriq doesn't parse code itself (a scope cut): reading code is the agent's
job, structure comes from paths and history.

## Code

`cofabriq.loop.refine.refine`
