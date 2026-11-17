# ADR-013: Local tickets and one ticket key

## Status

Accepted, 2026-11-17 (0.9).

## Context

Until 0.9 every ticket was a GitHub issue, and its number named everything
the loop made from it: the flow's thread (`owner/name#51`), the run's file
stem, the worktree, the branch, the API routes. A dev's own repo may have
no GitHub issues, or none they want to give cofabriq a token for, so 0.9
adds tickets written locally (`cofabriq ticket new`, the UI's "Write a
ticket"). Their numbers start at 1 too: a local ticket 3 and GitHub's #3
would get the same thread, the same branch, the same files.

The number is in every stored record, spec and checkpoint since 0.1.

## Decision

- **The stored shape stays.** `Issue.number` is still an int; a new field
  `source` (`github` or `local`) says whose number it is. Records, specs and
  checkpoints from before have no `source` and load as `github`.
- **One key, computed.** `ticket_key(source, number)` gives `51` for
  GitHub's #51 and `L3` for the third local ticket; `parse_key` goes back.
  Everything that names a ticket outside the store uses the key: thread
  `owner/name#L3`, run stem `<slug>-L3-<stamp>`, branch `cofabriq/L3-<slug>`,
  routes `/api/flows/{owner}/{name}/{ticket}`.
- **Checked at the edges.** The key becomes part of paths and git refs, so
  every CLI argument and route parameter is matched against one pattern
  (`^L?(0|[1-9][0-9]*)$`) before anything uses it.
- **A local ticket never reaches GitHub.** It is never embedded with the
  issues, its PR body says it is local instead of `Fixes #3`, and `--apply`
  refuses it.

## Consequences

- No migration: every file written before 0.9 still loads, its plain
  number is its key.
- Two names for one thing in the API: `number` (the stored int) and
  `ticket` (the key). The UI shows the key.
- `IssueRef` (duplicates, impact) stays GitHub-only: local tickets take no
  part in retrieval.
- Another tracker (Jira, Linear) is one more `source` literal and one more
  prefix, the way their own keys (`ENG-123`) work.

## Alternatives considered

- **A string id replacing the number.** The cleanest model, but a migration
  of every stored record, spec and checkpoint, for a name that can be
  computed.
- **Local tickets numbered from a reserved range (1,000,000 up).** No code
  changes at the edges, but it collides once a repo's issues get there, and
  nothing in `#1000003` says it is local.
