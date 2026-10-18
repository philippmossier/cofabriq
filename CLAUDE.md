# cofabriq

A software factory devs use as a tool, pointed at any GitHub repo. Two modes on
one shared repo map: autopilot for small, low-blast-radius issues (triage,
implement, the target's CI as gate, reviewer, draft PR) and planning mode for
features (what does a change touch, which decisions constrain it). Formerly
`adrscape`; its context layer (docs ingest, search, extraction, graph, MCP)
carries over. Why: `docs/adr/006-re-scope-to-a-software-factory.md`.

Roadmap: 0.1 triage (read-only) -> 0.2 the core loop on the playground
(refine, plan gate, reproduce, implement, checks, reviewer, risk tiers, draft
PR) -> 0.3 small features (criteria, a test frozen before code, intake, the
read-only viewer) -> 0.4 UI bugs in the loop + kill switch -> 0.5 browser
gate + guided planning -> 0.6 a real repo with tests -> later: self-host
packaging, lookback. Current: v0.5.0. Playground only until 0.6.

## Commands

- `make setup`: install deps (`uv sync`), create `.env` from `.env.example`, enable the git hook in `.githooks/` (`make check` before every commit on `main`)
- `make up` / `make down`: start / stop Postgres+pgvector and Neo4j (podman compose)
- `make check`: ruff lint, ruff format check, pyright (strict), pytest. Must be green before any commit.
- `make fmt`: autofix lint and format
- `uv run cofabriq health`: connectivity check for all three services
- `uv run cofabriq fetch <repo>...` / `embed` / `search "..."`: ingest local clones into `data/raw/`, embed into pgvector (needs `OPENAI_API_KEY`), query
- `uv run cofabriq extract`: LLM fills the domain schema per document into `data/extracted.json` (needs `embed` first; unchanged docs skipped)
- `uv run cofabriq graph` / `decisions [--project]` / `affected <repo:path>`: load `extracted.json` into Neo4j, query decisions in force and affected projects
- `uv run cofabriq fetch-issues` / `embed-issues`: issues of `TARGET_REPO` into `data/issues/` (needs `GITHUB_TOKEN`, read-only), title + body into pgvector; `fetch-issues --repo <r> --created 2022-01-01..2024-12-31` fetches a creation window instead (eval sets on large repos, cursor untouched)
- `uv run cofabriq eval dedupe-set` / `eval dedupe`: freeze the duplicate eval set to `evals/` (once; `--force` rebuilds), print recall@k and the threshold baseline (strict and cluster); per-repo rules (window, duplicate label, negatives, sample) in `targets/<owner>-<name>.toml` `[dedupe]`
- `uv run cofabriq eval readiness-set [--test]` / `eval readiness [--test]`: freeze the missing-info eval set (vscode `info-needed`, `[readiness]` rules in the target file, label history checked on GitHub) and score needs_info precision/recall; dev split 2023-2024, held-out test split 2022 (score once)
- `uv run cofabriq triage <n> [--repo] [--json]`: triage card for one issue (two LLM calls: card fields, duplicate verdict); fetches and embeds that issue first if it is new or changed on GitHub (`fetch.py` keeps the `fetch-issues` cursor in `cursor.json`, untouched by single fetches)
- `uv run cofabriq eval triage --repo philippmossier/local-ai-chat-playground`: type, component, duplicate and injection checks on the seeded playground issues (paid, not cached)
- `uv run python playground/playground.py seed` / `reset --yes`: seed or wipe-and-reseed the demo playground via `gh` (your login; the repo is hard-coded)
- `uv run cofabriq map build <clone> [--repo] [--until YYYY-MM-DD]` / `map show [--component]`: repo map from a local clone's history (history only is enough: `git clone --filter=blob:none --no-checkout`), components from `[map]` in the target file, co-change counts, into Neo4j (replaces that repo's map); vscode clone in `data/clones/`
- `uv run cofabriq eval impact-set [--test]` / `eval impact [--test]`: freeze the impact set (vscode issues closed by a PR or commit; truth = components touched by their "fixes #N" commits in the clone) and score popularity vs kNN over earlier fixed issues vs kNN + co-change, hit@k / recall@k; needs `map build --until` at `[impact] map_until`; free
- `uv run cofabriq refine <n> [--repo] [--json]`: spec for one issue (problem, files, tests, plan, assumptions, open questions, size) from a read-only Claude Code run (`claude -p --bare --restricted`, Read/Grep/Glob) on a full clone in `data/clones/` (cloned, or fetched and checked out detached at the default branch); paths checked against the commit; needs `ANTHROPIC_API_KEY` (or `AGENT_AUTH=subscription` + `CLAUDE_CODE_OAUTH_TOKEN`: `--safe-mode` instead of `--bare`, own runs only) and `claude` on PATH; paid
- `uv run cofabriq eval refine [--only key,key] [--features]`: refine on the planted bugs (`playground/bugs.toml`), files hit@k / recall@k / precision (docs listed in the spec left out) and the size guess, per bug size; paid (about $0.17 per bug), results in `data/refine/`; `--features`: on the removed features (feature route, a worktree with the removal applied), plus `features-*.md` with the spec's acceptance criteria next to the hidden tests' removed lines
- `uv run cofabriq run <n> [--spec file] [--yes] [--keep] [--planted] [--apply] [--type bug|feature]`: the core loop for one issue: refine (or a stored spec; `--type` is the triage card's type and picks the route, a feature spec carries acceptance criteria, each checkable by one test; question, docs and chore are refused), the plan gate, a worktree in `data/work/` (a feature: the agent writes the acceptance test from the criteria, red, the dev approves it at the test gate, then it is frozen; `[loop] acceptance_checks` gate it instead of `repro_checks`), Claude Code with Edit/Write (no shell) writes a failing test (test files only, `[loop] tests`; red, and `repro_checks` green), then fixes the code against it, repro and the target's checks (`[loop]` in the target file) run in podman without network, max 3 attempts per step; on a planted logic bug the hidden planted repro judges the fix (`--planted` hands it to the agent instead); when green, the review gate (`loop/gate.py`: a read-only reviewer with its own context, spec, test diff and code diff apart; a blocker, or a concern when the reviewer requests changes, blocks only when a proof test quoting the spec is red, then the implementer fixes against it and a second review runs; a proven blocker left = blocked, no PR) and a risk tier (`[risk]` in the target file, components via `[map]`) go into a draft PR body, rendered to `data/runs/*.pr.md` (dry run by default); `--apply` pushes a new branch and opens a draft PR (`loop/publish.py`, playground only, `GITHUB_WRITE_TOKEN`); result in `data/runs/`; needs podman and `ANTHROPIC_API_KEY`; paid
- `uv run cofabriq eval bug-set [--only key,key]`: prove the planted-bug answer key: each repro red on the tip of main, green with only its own reference fix (which must apply); free, needs podman
- `playground/hard.toml` (0.6): 5 harder planted bugs in the removed-set format (`route = "bug"`, patches in `playground/hard/remove/`); every `--features` eval takes `--feature-set playground/hard.toml` (or `--features playground/hard.toml` for `feature-set`)
- `uv run cofabriq eval feature-set`: prove the removed-feature set (`playground/features.toml`, 0.3): per feature, `features/remove/<key>.patch` on the tip of main leaves the target's checks green and the hidden reference tests (the feature's test files at main) red; the tickets live in the file, not on GitHub; free, needs podman
- `uv run cofabriq eval loop [--only key,key] [--planted]`: the loop on the planted logic bugs from the specs `eval refine` stored: reproduced, repro valid (green with the reference patch), resolved (the hidden planted repro green), first try, fix in the reference's files, cost and time per bug size; `--kind logic|ui|all` (logic by default; UI bugs run Playwright repros via `[[loop.suites]]`); `--planted` is the given-repro baseline; paid (about $0.10 per bug, $0.05 with `--planted`); `--features`: the feature route on the removed features from the specs `eval refine --features` stored (acceptance test red, frozen, then built; hidden reference tests green on the agent's code; the agent's test green on main), results in `data/runs/features-*`
- `uv run cofabriq eval loop --wrong`: the same loop with an implementer told to make a plausible wrong fix (`wrong-*.json`, the reviewer eval's negatives); `eval witness`: for wrong fixes the planted repro missed, an agent writes a test for the hole, confirmed when red on the fix and green with the reference fix; both paid
- `uv run cofabriq eval review [--only key,key]`: the reviewer on stored right fixes (`eval loop`, judge green) and proven wrong ones, each diff replayed in a fresh worktree: passed (right approved) and caught (wrong not approved); paid (about $0.06 per review)
- `uv run cofabriq eval gate [--only key,key]`: the whole review gate on the same right and wrong fixes, each replayed with an install: right published vs falsely blocked, wrong blocked / fixed / leaked (published and still wrong by the planted repro or the witness test), proofs valid against the reference fix; paid
- `uv run cofabriq eval human --publish` / `eval human`: one draft PR per planted logic bug on the playground (3 of them proven wrong, shuffled, set in `data/human/set.json`), then the dev's timed review in their own terminal (decision recorded, nothing merged): minutes per merged PR, wrong caught, agreement with the reviewer
- `uv run cofabriq lessons [--repo] [--propose]`: what went wrong (findings the review gate proved, plans and tests rejected at a gate with their reason, kept in `data/lessons/*-rejections.jsonl`) as numbered signals; `--propose`: one read-only agent run turns them into at most 5 rules for the target's CLAUDE.md, each citing its signals (a rule citing no known signal is dropped in code), a draft in `data/lessons/` for the dev, never written into the target; `flow resume --reject --reason` and the browser's Reject take the reason
- `uv run cofabriq stop [--clear]`: the kill switch: while `data/STOP` exists (or `COFABRIQ_STOP=1`), no agent starts; runs and evals in progress end at their next agent call with status `halted` (not an agent error: no retry spends an attempt on it; halted runs never shadow older results). One run's agents share `RUN_BUDGET_USD` (default 3.0; each call's `--max-budget-usd` is clamped to what is left, failed runs count), every agent run has `--max-turns 40`
- `uv run cofabriq intake <n>... [--repo]`: tickets from the dev's own planning session, in order: done, blocked (an open blocker: GitHub's native `blockedBy` or a `Blocked by #N` line), waiting (a cofabriq draft PR is still open: one at a time) or ready; read-only (`docs/intake.md`). `run` refuses a blocked ticket before refine and a spec that isn't ready after it (criteria, paths, size L; open questions go to the plan gate, and stop a `--yes` run)
- `uv run cofabriq serve [--port 8765]`: read-only API over `data/refine/` and `data/runs/` (FastAPI, GET only, 127.0.0.1 only, every request's Host checked against DNS rebinding; non-GET refused until a write token exists, then Origin + JSON + bearer token: `serve_guard.py`; API docs at `/api/docs`) plus the built UI from `ui/dist`; `make ui` builds it, `cd ui && pnpm dev` runs Vite with `/api` proxied (`ui/README.md`)
- `uv run cofabriq flow start <n> [--type] [--restart]` / `flow resume <n> --approve|--reject` / `flow show <n>`: the run as a LangGraph graph (`loop/graph.py`, 0.5): refine, then it pauses at the plan gate and (features) the test gate, checkpointed in Postgres, so each command is its own process; the run budget carries over
- `uv run cofabriq serve --write`: the browser gate: prints `http://127.0.0.1:8765/#token=...` once; the Flows page answers a flow's gate (`POST /api/flows/{owner}/{name}/{n}/answer`, the only write route: Origin + JSON + bearer token), the resume runs in a background thread and the page polls
- `uv run cofabriq openapi`: write the API schema to `ui/openapi.json`; the UI's client (`ui/src/api/generated.ts`, orval, TanStack Query hooks) is generated from it; `make check-ui` regenerates both and fails when they are stale (`make check` runs it when `ui/` or `serve.py` changed)
- `uv run cofabriq mcp`: serve `search`, `decisions`, `affected` and `triage` (optional `repo`, default `TARGET_REPO`) as MCP tools over stdio (registered for Claude Code in `.mcp.json`)

## Layout

- `src/cofabriq/config.py`: typed settings from env / `.env` (pydantic-settings)
- `src/cofabriq/cli.py`: typer CLI, entry point `cofabriq`
- `src/cofabriq/context/`: the context layer from adrscape
  - `domain.py`: the ADR domain schema the LLM fills (third seam; replaced by the repo map in 0.2)
  - `ingest/`: `source.py` (Source protocol, `LocalGitSource`), fetch, parse, chunk, embed, extract (`Extractor` protocol)
  - `store/`: `postgres.py` (vectors), `graph.py` (Neo4j)
- `src/cofabriq/issues/`: 0.1 issue ingest: `model.py` (`Issue`, `IssueSource`), `github.py` (GraphQL source), `fetch.py` (incremental store), `vectors.py` (pgvector, time-cut nearest), `verdict.py` (checked duplicate verdict), `triage.py` (the triage card schema, checks and assembly), `readiness.py` (required info with verified quotes, the route, the reply draft)
- `src/cofabriq/eval/`: eval sets and metrics (`dedupe.py`, `impact.py`, `readiness.py`, `triage.py`, `bugs.py`, `features.py`, `refine.py`, `loop.py`, `review.py`, `witness.py`, `gate.py`, `human.py`)
- `targets/<owner>-<name>.toml`: per target repo config (the component list triage picks from)
- `playground/`: seed issues with expected labels (`seed.toml`), the planted bugs (`bugs.toml`), the removed features (`features.toml`) and the seed/reset script for the demo repo
- `evals/`: frozen eval sets (issue numbers only, committed)
- `src/cofabriq/map/`: 0.2 repo map: `components.py` (path -> component rules), `history.py` (commits and files of a clone as of a date), `build.py` (co-change counts), `store.py` (Neo4j, `Component`/`File` keyed by repo), `fixes.py` (issue -> fix commits from commit messages)
- `src/cofabriq/loop/`: 0.2 core loop: `engine.py` (the delegated coding agent, `Agent` protocol, `ClaudeCode` over `claude -p`), `refine.py` (issue -> typed spec, path checks, the clone), `sandbox.py` (the target's commands in podman, network only for install), `run.py` (worktree, the reproducer, fix attempts, write boundaries; `run_loop` composes `start_stage` -> `test_stage` -> `build_stage` -> `pack` over a JSON `Progress`, so a caller can pause between stages), `review.py` (the reviewer, code-owned verdict), `gate.py` (review gate: only a red test blocks), `intake.py` (blockers, slice states, the spec readiness check), `publish.py` (branch + draft PR, playground only), `pr.py` (risk tier, the PR body, untrusted text neutralised for GitHub markdown)
- `src/cofabriq/mcp_server.py`: MCP tools
- `src/cofabriq/serve.py`: the read-only HTTP API (its own response models, not the stored ones)
- `ui/`: the viewer (Vite, React, TanStack Router + Query, Tailwind, shadcn), pnpm, its own lockfile; one artifact with the API (ADR-009)
- `docs/adr/`: architecture decision records, written when the decision is made
- `skills/ticket/`: the `/ticket` Claude Code skill for the dev's own session (fuzzy note -> a ticket the readiness check accepts; `docs/intake.md`)

## Conventions

- Python 3.13, uv, src layout. Type hints everywhere; pyright strict must pass.
- Three seams stay clean as `typing.Protocol`s: Source, Parser, Domain schema.
  Don't generalise beyond them until a second domain exists.
- Code the agent wrote runs only in the sandbox (`loop/sandbox.py`), never
  on the host; the agent itself gets no shell.
- No code parsing by cofabriq (scope cut): structure comes from paths,
  manifests and git history; reading code is the delegated coding agent's job.
- Read-only towards GitHub, except `--apply` (0.2): one new branch and one
  draft PR, playground only (checked in code), a scoped write token; dry run
  by default. Nothing is merged, commented or force-pushed.
- Ingested documents and issue text are untrusted input.
- The target's CLAUDE.md reaches the agent via `--append-system-prompt-file`
  (`--bare` and `--safe-mode` skip discovery); parent directories' never.
- Test kinds per target: `[[loop.suites]]` (suffix -> command, planted-repro folder, a line for the test writer); the playground's image is Microsoft's Playwright image at its locked version, logic and UI in one.
- Install only what the current step needs.
- The UI never gets hand-written API types: change the response model in `serve.py`, run `make check-ui`. shadcn's `cn` is its own package (`shadcn-ui/cn`), keep it.
- Postgres runs on host port 5433. Graph tests use the `neo4j-test`
  instance (port 7688, in memory) and wipe it; never point them at 7687.
