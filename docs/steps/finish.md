# Finish: clean up

The throwaway worktree is removed (unless `run --keep`).

## Why

The worktree held code an agent wrote; nothing of it stays on disk once the
change is recorded. The change itself lives on in the record's diff and, with
`--apply`, in the draft PR.

## Code

`cofabriq.loop.worktree.remove_worktree`
