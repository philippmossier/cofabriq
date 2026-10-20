# Evals

Every retrieval or agent change gets a number. The 0.2 loop is measured on
bugs planted in a demo app (truth by construction), triage on real issue
histories (frozen snapshots).

## 0.2: the loop on planted bugs

The [playground](https://github.com/philippmossier/local-ai-chat-playground)
(a small local-first chat app) has 22 planted bugs, each filed as a
user-style issue: 10 logic bugs with a vitest repro, 12 UI bugs with a
Playwright repro (15 small, 6 medium, 1 large). Each repro is red on the
planted code and green with only its own fix, so the truth is known by
construction and can't be in any model's training data. The loop runs on
the 10 logic bugs; UI repros aren't in the loop yet.

| Stage (Claude, one model in every role)                  | Result                                                     | Cost per bug      |
| -------------------------------------------------------- | ---------------------------------------------------------- | ----------------- |
| refine, all 22: spec names the fix's file                | hit@1 1.00, precision 0.88; size right 16/22               | $0.18, 34 s       |
| loop, 10 logic bugs: own failing test, fix               | reproduced 10/10, test valid 10/10, resolved 10/10, all first try | $0.105, 97 s |
| reviewer alone: 10 right fixes, 9 proven wrong           | right approved 10/10, wrong rejected 9/9                   | $0.057            |
| review gate: 10 right fixes, 8 proven wrong              | right published 10/10; wrong fixed 8/8, leaked 0; 10 proof tests, all valid | $0.137 |
| human round: 10 draft PRs, 3 wrong, before the gate      | 1.2 min per PR (median), 1.6 per merged PR; wrong merged 2/3 (skimmed, no local tests) | -               |

End to end, a right fix costs about $0.34 (refine, loop, one review) and
about 2.5 minutes of agent time; the dev's part is reading the spec at the
plan gate and the draft PR. Human minutes per merged PR: 1.6 leaving out
the first PR, whose 9.5 minutes went into changing the body format; 2.5
with it.

"Proven wrong": an implementer told to make a plausible wrong fix (wrong on
purpose, green on its own test). The planted repro caught only 1 of 10,
since it covers the core case only, so a third agent writes a witness test
for the hole; a fix counts as wrong when the witness is red on it and green
with the reference fix (7 or 8 of 9 confirmed, the witness isn't
deterministic). "Valid" proof test: green with the reference fix, so it
encodes a right reading of the spec.

What it says: the mechanism works, the sets don't discriminate. Every
stage is saturated: one-file bugs in a small repo, a spec that names the
cause, the same model writing, sabotaging and reviewing, and wrong fixes
that are sabotage, not natural mistakes. The human round is one reviewer
(me, who also planted the bugs) on 10 PRs, who skimmed without testing
locally: an anecdote, not an eval. Next difficulty bars: UI bugs,
real issues, a different reviewer model.

Reproduce: `cofabriq eval refine`, `eval loop`, `eval loop --wrong`,
`eval witness`, `eval review`, `eval gate` (all paid, results in `data/`).

### 0.2.1: with the target's CLAUDE.md

Until 0.2.1 the agent never saw the playground's CLAUDE.md (`--bare` skips
discovery). The same evals with it loaded, run on a Claude plan
(`AGENT_AUTH=subscription`, so `--safe-mode` instead of `--bare`). Costs are
API list prices the CLI reports, not money spent, and safe mode's larger
system prompt inflates them: compare times and rates, not dollars.

| Stage                       | 0.2.0 (`--bare`, no CLAUDE.md)                     | 0.2.1 (`--safe-mode`, CLAUDE.md)                                    |
| --------------------------- | -------------------------------------------------- | ------------------------------------------------------------------- |
| refine, all 22              | hit@1 1.00, precision 0.88; size 16/22; 34 s       | hit@1 1.00, precision 0.91; size 15/22 (all 6 M called S); 27 s     |
| loop, 10 logic bugs         | 10/10 reproduced, valid, resolved, first try; 97 s | 10/10 reproduced, valid, resolved, first try; 98 s                  |
| review gate: 10 right, 8 wrong | right published 10/10; wrong fixed 8/8, leaked 0 | right published 10/10; wrong fixed 7/8, **leaked 1**; 9 proofs, all valid |

The leak is the first miss on any set: on the same stored wrong fix
(`reload-active-chat`, a `messages.length > 0` check the spec never asked
for) the reviewer names the exact defect both times, but now files it as a
concern instead of a blocker, 3 runs out of 3. The gate only tried to prove
blockers, so an unproven concern published. Whether the CLAUDE.md or safe
mode's system prompt moved the severity isn't separated yet.

The fix is in the gate, not the prompt: severity is the model's label, so
it no longer decides alone what gets proven. When the reviewer asks for
changes, its concerns get a proof test too (nits never); the spec quote
still turns an opinion into "nothing to prove". Same 18 fixes after it:
right published 10/10; wrong fixed 8/8, **leaked 0**; 17 proof tests, all
valid; $0.289 per case notional ($0.201 before: the extra proofs land on
the wrong fixes only, every right fix is approved).

## 0.6: a harder set that wasn't harder

Every stage was saturated, so 5 harder bugs were planted (`playground/
hard.toml`, the features mechanism on the bug route): tickets that describe
only the symptom; a cause in a validation filter, in data, in a shared
helper; two wrong constants in two files; the same mistake in two places.
`eval feature-set` proves them: 5/5, checks green with the plant, the
hidden tests red.

| Stage (Claude, plan) | Result | Per bug |
| --- | --- | --- |
| refine: the fix's file first | 5/5 (both files of the two-file bug), precision 1.00, size 4/5 | 30 s |
| loop: own test red, then fixed | 5/5, all first try; hidden tests green 5/5; agent's test valid on main 4/5 | $0.22, 93 s |

None of it was hard for the model on a small repo. The one invalid test is
stricter than the reference: it asks for "de-DE" too, which main never
handled (the app only passes "de"), and the fix accepts every German
variant. What would discriminate is out of the playground's reach: a much
bigger codebase, a requirement that needs a judgement call, a fix that is a
design decision. So the set stays as a regression check, not as a
benchmark.

**Lessons** (`cofabriq lessons --propose`): from 36 signals on the
playground (all findings the review gate proved), one agent run proposed 5
rules for its CLAUDE.md, each citing 3 to 18 signals, none dropped for
citing unknown ones ($0.12). The strongest says what most proven findings
had in common: make exactly the change the spec describes, no extra guards
or heuristics, raise doubts as open questions. Not measured yet: whether a
rule stops a repeat. The signals come from wrong-on-purpose fixes, and on
these sets the loop makes no natural mistakes to repeat.

**The privacy sentinel** (`[sentinel]`, checked in code before any review):
the playground's contract is "nothing typed leaves the device", and its
allow-list is its CSP. A new URL whose host the CSP doesn't allow, or any
change to the CSP, blocks the run. On the 50 stored right changes (logic,
UI, features, hard bugs): 0 false alarms (`eval sentinel`). The leaks it
must catch (a telemetry fetch, a web font, a lookalike host, a widened
CSP) are unit tests; URLs in comments, tests and docs are left alone.

**Screenshots as evidence** (`[loop.evidence]`): after a green UI run the
agent's browser test runs once more with screenshots on, through an
override config in the ignored `test-results/` (the diff stays clean).
Live on the planted "New chat" bug (#67): 2 screenshots in the viewer and
a line in the PR body. That run was also the first live retry: the first
fix left the browser test red, the second turned it green.

## 0.5: runs that wait, approved in the browser

0.5 adds no new eval set; what it changes is checked by running it.

| Check | Result |
| ----- | ------ |
| LangGraph spike, against criteria set before it | ~300 lines on the loop's stages; resume live across 3 processes; evals untouched (they call the same stages) |
| a flow approved in a headless browser | #54 (logic) and #69 (UI bug): done, review approved, recorded for the viewer |
| the whole path, live | rough note -> /ticket -> #83 -> intake ready -> plan and test approved in the browser -> green first try, review approved, PR body; $0.14 for test and build |
| the browser gate's trust boundary | a rebinding Host 403, another page's Origin 403, no or wrong token 401, read-only without a token 405 (unit tests and live) |

One real run is a demo, not a rate. What it showed: the flow skipped the
readiness check `run` does, and a feature's PR title said "Fix"; both
fixed in 0.5.

## 0.4: UI bugs in the loop

The 12 planted UI bugs (a button that never enables, a sidebar sorted the
wrong way, a link that doesn't open a new tab) only show in a browser. The
sandbox now runs Microsoft's Playwright image at the playground's locked
version; the app runs on its fake engine (no model, no GPU) and Vite on
localhost, so the tests need no network. A target declares its test kinds
(`[[loop.suites]]`: vitest for `*.test.ts`, Playwright for `*.spec.ts`)
and the test writer picks the one that can show the bug.

| Stage (Claude, plan, CLAUDE.md loaded)      | Result                                                           | Per bug      |
| ------------------------------------------- | ---------------------------------------------------------------- | ------------ |
| answer key (`eval bug-set`, free)           | 22/22 repros red on the planted code, green with only their fix  | -            |
| loop, 12 UI bugs: own failing test, fix     | reproduced 12/12, resolved 12/12, first try 11/12; test valid 9/12 | $0.21, 99 s |

"Valid": the agent's test is green with the reference fix alone. The
three misses have one cause: the test needed a neighbouring planted bug
fixed, and the agent fixed it on the way (two sidebar tests open a second
chat through the planted "New chat" bug; the stop test reads a footer
another planted bug hides). On logic bugs this never happened; UI flows
share screens, so planted UI bugs step on each other.

`eval bug-set` was added after the first run marked a right test invalid:
one reference patch had been made on top of another bug's fix and didn't
apply to the planted code. Rebuilt, the key proves 22/22; that test,
re-checked against the rebuilt patch, is valid (the stored run still says
8/12).

### Kill switch

No agent starts while `data/STOP` exists (`cofabriq stop`) or
`COFABRIQ_STOP` is set; runs in progress end at their next agent call with
status `halted`, which is not an agent error, so no retry spends an attempt
on it. One run's agents share a budget (default $3; each call's own cap is
clamped to what is left, failed calls count), and every agent run has a
40-turn cap (5-13 in practice). Tested: unit tests for each, and live,
`cofabriq stop` then `eval loop` ended the bug with no agent call ($0.00,
the install only).

## 0.3: the feature route

Features take the same loop once the spec is the truth. The playground has
no feature history to revert (one squashed commit), so 9 small features
(8 S, 1 M, all logic) were taken out of the tip of main instead, the planted
bugs run backwards: a patch strips the code, its test cases and the doc lines
that describe it. The reference is main, and the feature's test files there
are the hidden judge. `cofabriq eval feature-set` (free) proves each
removal: every check green without the feature, the hidden tests red.

| Stage (Claude, plan, CLAUDE.md loaded)        | Result                                                        | Per feature |
| --------------------------------------------- | ------------------------------------------------------------- | ----------- |
| refine, 9 features: spec names the files      | hit@1 1.00, precision 0.85; size right 9/9; 7 criteria median | 35 s        |
| loop, 9 features: acceptance test, then build | test red 9/9; built 9/9, all first try; hidden tests green 9/9; agent's test green on main 7/9 | 94 s |
| readiness (intake, in code): the 9 specs       | 9/9 pass; 4 bring open questions to the plan gate             | free        |

Docs the spec lists are left out of precision, as they are left out of the
truth (a feature's spec rightly names the doc it updates; with them, 0.65).
The acceptance test is written from the approved criteria, has to be red,
and is frozen: the builder can't change it. "Hidden tests green": the
feature's original tests pass on the agent's code, so the criteria and the
build say what the feature was. "Agent's test green on main": the agent's
test passes on the original code, so it tests the feature as it was built.
Both misses there are not misreads. In room-to-grow the test pins the
too-slow advice to the lighter model, which is planted bug
`too-slow-suggests-lighter`: main is wrong, and the build fixed it on the
way. In amd-strong-gpu the test uses AMD fixtures the build added to a
helper file, which main doesn't have.

Open questions don't fail the check: they go to the plan gate, where the
dev's approval answers them (with `--yes` they stop the run). The four
specs that carry them ask things a dev should answer, and three point at a
planted bug next to the feature: should the too-slow advice be fixed in the
same PR, which chat is "first" when the sidebar sorts the other way, and
whether weekday names near the 7-day edge are ambiguous. The fourth quotes
the playground's CLAUDE.md ("do not hard-code behaviour on a GPU vendor
name") against the AMD rule. The loop numbers above ran all nine anyway.

Same caveat as the bugs: small features in a small repo, one model writing
criteria, test and code, so a misread feature would go green on all three;
the hidden tests are the only outside judge here, the dev's look at the
criteria and the test is it in a real run (`run --type feature` shows the
frozen test before any code).

## 0.2: impact from the repo map

A map of a repo from paths and git history only (components, co-change,
fix commits linked to issues). From the issue text alone, `impact` ranks
the components a fix will touch: on microsoft/vscode (405 components), a
component the fix touched is in the top 5 for 75% of issues on a held-out
year (2024, n = 300; hit@10 0.83). A popularity baseline got 22% on the 2023
split.
No LLM call. Kept as an MCP tool, not as the way the loop finds code: the
agent reads the code. Reproduce: `cofabriq eval impact [--test]`.

## 0.1: duplicates and missing info

Duplicate detection is measured on two real issue histories. Each query only
sees issues filed before it, and only comments written before it.

**microsoft/vscode, provable labels (0.1.2).** 62,467 issues (created
2022-2024, snapshot frozen 2026-10-15); queries are 2023-2024. A duplicate
counts only if a maintainer set vscode's `*duplicate` label and a human
comment names the target (`/duplicate #N`). A non-duplicate counts only if
it was closed as completed by a merged PR or a commit. 2025 on is left out:
maintainer accounts there post LLM-written triage comments, and labels
suggested by an LLM would make an LLM eval circular. The rules are in
[`targets/microsoft-vscode.toml`](../targets/microsoft-vscode.toml), written
before scoring; 200 pairs and 200 non-duplicates sampled with a fixed seed.

| Dedupe, vscode (n = 400), verdict 0.1.1      | Embedding threshold | LLM verdict      |
| -------------------------------------------- | ------------------- | ---------------- |
| `duplicate_of` precision                     | 0.212               | 0.346            |
| `duplicate_of` recall                        | 0.145               | 0.235            |
| precision / recall, any issue of the cluster | 0.372 / 0.255       | 0.515 / 0.350    |
| `unsure` (abstained), of which right         | -                   | 88, 11 (cluster) |
| cost / median latency per issue              | embedding only      | $0.00073 / 1.9 s |

What it says: about half of the `duplicate_of` calls are right, so the
verdict is a suggestion for the dev, never an action. Of the 136 calls, 32
hit a provable non-duplicate, and only 1 of those shares a fix with its
target. Most are a narrow follow-up called a duplicate of the broader
feature issue ("Fine tune `clear` alert" vs "Alert screen reader users that
something has occurred"). Retrieval caps the rest: in a 62k pool the target
is in the top 10 for 40% of duplicates (56% counting its cluster).

Known gaps of the set: 21% of otherwise usable pairs name a target from
before 2022 and are dropped, so long-lived issues are under-represented.
Non-duplicates closed by a PR lean towards team-filed work items, which are
harder than typical user reports.

Tried and dropped: hybrid retrieval (Postgres full-text search on the
query's rarest words, fused with the vector list by reciprocal rank). Top-10
recall moved within noise on vscode (0.40 to 0.42), dropped on open-webui
(0.568 to 0.561), and top-1 got worse on both.

**open-webui, maintainer marks (0.1.0, 0.1.1).**
[open-webui](https://github.com/open-webui/open-webui), 10,302 issues,
snapshot frozen 2026-10-13: 155 duplicate pairs from GitHub's duplicate
marks and human "duplicate of #N" comments, plus 155 issues nobody marked.

| Dedupe, open-webui (n = 310)                 | Embedding threshold | LLM verdict 0.1.0 | LLM verdict 0.1.1 |
| -------------------------------------------- | ------------------- | ----------------- | ----------------- |
| `duplicate_of` precision                     | 0.250               | 0.346             | 0.434             |
| `duplicate_of` recall                        | 0.310               | 0.342             | 0.342             |
| precision / recall, any issue of the cluster | 0.292 / 0.361       | 0.399 / 0.394     | 0.484 / 0.381     |
| `unsure` (abstained), of which right         | -                   | -                 | 62, 4             |
| cost / median latency per issue              | embedding only      | $0.00066 / 2.2 s  | $0.00069 / 3.6 s  |

0.1.1 lets the verdict abstain (`unsure`: likely the same problem, but the
text can't confirm it) and asks for the same cause, not just the same
symptom. Both changed in one prompt version, so the gain isn't attributable
to abstention alone.

Retrieval: the original is in the vector top 5 for 49.7% of duplicates
(57.4% counting another duplicate of the same issue). The verdict can only
pick from the top 10, so retrieval caps it.

What the numbers say: the precision above is a lower bound, and the labels
are the bigger problem. A non-duplicate here only means "nobody marked it":
only 4 of the 155 were closed by a linked PR, so almost none is proven. A
hand check of the 63 claims the labels call wrong found 40 real duplicates
the maintainers never marked (or marked against another issue of the same
problem), 13 real errors and 10 unclear. The real errors are same-symptom,
different-cause calls (the same pip error on two Python versions, a
regression matched to the original feature request). That hand check was
an LLM's judgement plus a spot check, and the vscode set above corrected
it: on provable labels, the precision is about 0.5, not 0.8.

**Missing info, microsoft/vscode (0.1.3).** Does the card ask for more
exactly when a maintainer did? Positive: the maintainers labelled the
report `info-needed`. Negative: a `bug` fixed by a merged PR or a commit
that never carried that label (label history checked on GitHub). Both sides
filed through vscode's issue template (user reports, not team notes) with
an unedited body. 200 + 200 per split; the method was chosen on 2023-2024
and scored once on a held-out 2022 split.

| Needs info, vscode, held-out 2022 (n = 400) | Flagged | Precision | Recall |
| ------------------------------------------- | ------- | --------- | ------ |
| the model asks any question (0.1.2 card)    | 217     | 0.733     | 0.795  |
| required items without a verified quote     | 61      | 0.787     | 0.240  |
| questions the model marks blocking          | 70      | 0.871     | 0.305  |
| **route `needs_info` (either of the two)**  | 94      | 0.787     | 0.370  |

What it says: when the card routes to `needs_info`, a maintainer asked too
in about 4 of 5 cases, but it catches only about a third of the reports
maintainers asked about. Most maintainer asks are issue-specific ("a gif
would help", "does it happen on Insiders", "share a repo I can test
with"), which no checklist knows; the model's questions catch most of them
but also fire on 41% of bugs that were fixed as written, so they stay on
the card as questions, not as a route. On the dev split, blocking
precision was 0.868: the judgement held on unseen data. $0.0003 and 3.5 s
per issue.

Triage card, on the [playground](https://github.com/philippmossier/local-ai-chat-playground)
(24 seeded issues with 4 duplicate pairs and 4 injection attempts), three
runs of 0.1.1 (one run of 0.1.3: 24/24, 24/24, 4/4, 0/4): type 22 to 24/24, component 23 to 24/24, duplicates 3 to
4/4 (the miss is an `unsure` on the right issue) with no false
`duplicate_of`, injections followed 0/4, $0.0003 and 3.8 s median per
issue. When the card's type and the verdict disagree (typed `feature`, but
a duplicate of a bug), the card lists a conflict instead of picking a
winner: 1 conflict in 3 runs, on a wrongly typed card. This is a
sanity check, not a benchmark: the issues and their expected labels were
written together. Type accuracy on messy real issues is still open.

Reproduce: `cofabriq eval readiness --repo microsoft/vscode [--test]`,
`cofabriq eval dedupe --repo <repo> [--verdict]` (the sets are
committed in `evals/`; vscode needs `fetch-issues --repo microsoft/vscode
--created 2022-01-01..2024-12-31` first, about an hour) and
`cofabriq eval triage --repo philippmossier/local-ai-chat-playground`.
