---
name: ticket
description: Turn a fuzzy note about a bug or a small feature into a GitHub issue cofabriq can take (`cofabriq intake`, `cofabriq run`). Use when the user types /ticket with a rough sentence, or asks to write up a bug or feature as a ticket.
disable-model-invocation: true
---

# /ticket: a fuzzy note in, a ticket cofabriq can take out

The user gives a rough note ("shift enter sends the message, should be a new
line"). You turn it into one GitHub issue that cofabriq's readiness check
will accept, without inventing anything. You draft; the user files.

## 1. Understand, then ask (one question at a time)

- Bug or feature? A bug: something that worked or should work is broken.
  A feature: something new. Neither (a question, docs, a chore): say so and
  stop; cofabriq doesn't take those.
- Read the code just enough to name things the way the code does (the
  component, the function, the visible label). Don't plan the fix.
- If the repository has cofabriq's MCP tools, use them as context:
  `decisions` (an ADR or rule the change would touch: say so in the ticket),
  `search` (docs about this area), `impact` (likely components).
- Ask only what the ticket needs and the note doesn't say. For a bug: steps,
  expected, actual, where (browser, OS, version). For a feature: what should
  happen, for whom, what must stay as it is. Ask one question at a time and
  stop asking once the ticket is checkable.

## 2. Never invent

- No steps, versions, error messages or expected behaviour the user didn't
  give or the code doesn't show. A field you can't fill becomes an open
  question in the ticket ("Which browser?"), never a plausible guess.
- Keep the user's own words where they carry the meaning.

## 3. Write it so cofabriq's readiness check passes

The check (`cofabriq run`, `docs/intake.md`) sends a spec back when it has no
criterion a test can check, names files that don't exist, or is too big for
one PR. So:

- **One slice.** If it needs more than a few places in the code, split it
  into tickets and link them (`Blocked by #N`, or GitHub's own "blocked by").
- **Checkable behaviour.** For a feature, 2-6 criteria, each a concrete
  situation and the exact expected result ("`detectLang(["en-US", "de"])`
  returns `"de"`"), plus what must stay as it is.
- **Name the interface** a test will call when the feature adds one (a new
  return kind, a new function): the acceptance test is written against it
  and frozen.
- No implementation plan: cofabriq's refine writes that from the code.

## 4. The ticket

Use the repository's own template if it has one
(`.github/ISSUE_TEMPLATE/`). Otherwise:

Bug:

```markdown
<one sentence: what is wrong>

**Steps**
1. ...

**Expected:** ...
**Actual:** ...
**Where:** <browser, OS, version, if known>

**Open questions**
- <anything only the user can answer, or none>

<details><summary>Original note</summary>

<the user's note, verbatim>
</details>
```

Feature:

```markdown
<one sentence: what is wanted and why>

**What**
- <criterion: situation -> exact result>
- <what must stay as it is>

**Interface** (only if a test must call something new): ...

**Open questions**
- ...

<details><summary>Original note</summary>

<the user's note, verbatim>
</details>
```

The original note stays in every ticket: when a spec comes out wrong later,
it shows whether the ticket or the refine lost something.

## 5. Show, then file

Show the title and body. File only after the user says so:

```bash
gh issue create --repo <owner/name> --title "<title>" --body-file <file>
```

Then tell them the next step: `cofabriq intake <n>` (order and readiness) or
`cofabriq run <n> --type bug|feature`.
