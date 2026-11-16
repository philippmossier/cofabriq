# ADR-012: Your own repo, read-only: cloned from your clone, a proven config, the result pulled

## Status

Accepted, 2026-11-16 (0.9).

## Context

Until 0.8 the loop ran on one repo, the playground, through a target file
written by hand and a draft PR pushed with a write token scoped to that
repo. For someone who clones cofabriq and points it at their own repo, three
things stood in the way:

- **The config.** `[loop]` (image, install, checks, test globs, suites) is
  the contract the whole loop rests on, and only `/api/repos` repos that
  have one show up. Nobody else knows how to write it.
- **The token.** `refine` cloned from `https://github.com/<repo>` without a
  token, so a private repo failed. The obvious fix, reusing `gh auth
  token`, hands cofabriq the `repo` scope, which can write: "cofabriq never
  holds a write token" would stop being true the moment it is used.
- **The result.** `--apply` is a write to GitHub, playground only, checked
  in code. A dev's own repo needs another way back.

## Decision

- **The code comes from the dev's local clone, not from GitHub.** `repo
  add <path>` records `[clone]` (the path and the base branch) in the
  target file; cofabriq's own clone fetches that branch from that path. No
  token, private repos work, and the base of every run is the dev's local
  branch as it is now. A note says when it is behind its upstream, from the
  refs the dev already has (no network).
- **The config is proposed, then proven, never trusted.** A read-only
  agent proposes `[loop]` from the repo's manifests, Makefile and CI; each
  command cites the file and line it came from, and a quote not in that
  file is flagged. The dev reads and edits the file; nothing runs before.
  `repo check` then proves it in the sandbox on the base commit: the
  install, every check green, and per suite a canary test that must go red
  as written to fail and green as written to pass. Without the red
  canary, a suite command that passes no matter what would make the frozen
  red test, and so every run, worthless; without the green one, a suite
  that can't run at all would look red for the right reason. The proof
  carries the target file's hash: an edit takes it back. `run`, `flow
  start` and `/api/repos` refuse an own target that isn't proven as it is.
- **The result is pulled, not pushed** (0.9 step 2): the test and the fix
  as a branch in cofabriq's clone plus a patch; the dev fetches it into
  their clone and pushes it themselves. cofabriq writes nothing outside its
  own folders, and nothing to GitHub.
- **Own target files live in `targets/own/`** (git-ignored), next to the
  shipped ones, and win over a shipped file of the same name. A shipped
  target needs no proof: it is reviewed in git and proven by its evals.

The claim this buys: cofabriq never writes to your GitHub, and the loop on
your code needs no token at all. Importing issues (0.9 step 3) needs a
read-only one.

## Consequences

- Onboarding a repo is one paid agent run (about $0.40) and one free
  check, minutes; `eval repo-add` measures how often the proposal is proven
  unedited on repos of different stacks.
- The sandbox had to serve toolchains other than pnpm: a shared cache
  volume (`XDG_CACHE_HOME`, `GOMODCACHE`, npm's cache), labelled for every
  container (`:z`), else SELinux on podman's VM keeps the install's tools
  from the offline checks.
- The proposal can only be as good as what the container can do: no
  services (a database), no git, no network after the install. Checks
  that need them are left out, and say so.
- A run on a dev's repo starts from their local branch: if it is stale,
  the run is too. That is the dev's to fix; the note says so.

## Alternatives considered

- **`gh auth token` for the clone.** Works, but holds a token that can
  write; kept as a later opt-in for importing issues, with a warning when
  the token can write.
- **A config template per stack.** Covers only the stacks someone writes
  templates for; the agent reads the repo's own scripts and CI instead,
  and the check judges the result either way.
- **Trusting the proposal after the dev's review.** The dev reads
  commands, not whether a test runner collects a file in a folder; only a
  run shows that.
- **Writing the result branch into the dev's clone.** Their clone may have
  hooks, a dirty tree or a branch of that name; pulling leaves them in
  charge.
- **Forking the playground for newcomers.** The read-only quickstart
  already covers trying it.
