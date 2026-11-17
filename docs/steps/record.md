# Record the run

The run's result, the review and the draft PR body are written to `data/runs/`, where the viewer's Runs page reads them; a reviewed change also becomes a branch in cofabriq's copy of the repo and a patch.

## Why

Every run that got past setup is recorded, a red or halted one too: what went
wrong is the input for `cofabriq lessons` and for the evals. The draft PR body
is rendered here (a dry run by default); `run --apply` opens it as a draft PR
on the playground, nothing else.

A change that passed review (not blocked) is handed back without any write to
the dev's folder or to GitHub: two commits by cofabriq, the test first, on
`cofabriq/<n>-<slug>` in cofabriq's own clone (a new run on the issue replaces
it), and the same commits as `<stem>.patch`. The dev fetches the branch into
their folder, or applies the patch with `git am`, and pushes it themselves.

## Code

`cofabriq.loop.record.write_record`, `cofabriq.loop.handoff.hand_off`
