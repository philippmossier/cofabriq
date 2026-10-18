# Intake: from your planning session to the loop

cofabriq doesn't split big features. Planning stays in your own Claude Code
session, where you can be interviewed, push back and prototype; cofabriq
takes the tickets that come out of it. The chain follows Matt Pocock's
skills (github.com/mattpocock/skills): grill -> prototype -> spec ->
tickets.

```
your session:  grill -> prototype -> spec -> tickets on GitHub (blocking links)
                                                  │
cofabriq:      intake (order, readiness) -> run <n> --type feature -> draft PR
```

## Context for the planning session

`.mcp.json` registers cofabriq's MCP server, so the session can ask:

- `decisions`: the ADRs in force and how they relate ("is there a decision
  about how we pick models?")
- `search`: documents by meaning ("where is the privacy claim checked?")
- `impact`: which components a plan in plain text will likely touch, from
  the repo's history
- `triage`: the card for an existing issue (type, component, duplicates)

Text these tools return is quoted from the repositories: data, not
instructions.

## What a ticket needs

- One slice a draft PR can carry: size S or M. An L goes back to be split.
- Behaviour a test can check. Name the interface when the change adds one
  (`kind: "room-to-grow"`); the acceptance test is written against it and
  frozen.
- Its blockers as GitHub dependencies ("Blocked by" in the issue sidebar),
  or a `Blocked by #N` line in the body.

## What intake does

`cofabriq intake <n>...` reads the tickets, their blockers and cofabriq's
open draft PRs (read-only) and says per ticket:

| State   | Means                                                       |
| ------- | ----------------------------------------------------------- |
| done    | closed as completed                                         |
| blocked | a blocker is still open (not planned counts as open)        |
| waiting | a cofabriq PR is still open: one at a time, so each slice builds on a merged one |
| ready   | `cofabriq run <n> --type feature` may start                 |

`run` checks the same before paying for refine, and after refine checks the
spec in code: acceptance criteria present (features), the source files it
names exist (or are said to be new), size S or M. Anything else goes back
to you with the reason: fix the ticket, or edit the spec and pass it with
`--spec`. Open questions are shown at the plan gate: approving the plan
answers them (with `--yes` they stop the run).
