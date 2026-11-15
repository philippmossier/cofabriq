# Record the run

The run's result, the review and the draft PR body are written to `data/runs/`, where the viewer's Runs page reads them.

## Why

Every run that got past setup is recorded, a red or halted one too: what went
wrong is the input for `cofabriq lessons` and for the evals. The draft PR body
is rendered here (a dry run by default); `run --apply` opens it as a draft PR
on the playground, nothing else.

## Code

`cofabriq.loop.record.write_record`
