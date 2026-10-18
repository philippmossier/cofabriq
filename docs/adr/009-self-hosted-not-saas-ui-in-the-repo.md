# ADR-009: Self-hosted, not SaaS; the UI ships in this repo

## Status

Accepted, 2026-10-18.

## Context

0.3 adds a UI: a read-only viewer over the runs, later a browser gate for
the plan and test approvals. Two questions came with it: where the UI
lives, and what cofabriq becomes when someone besides me uses it.

A hosted service (sign in, connect your repo, runs on our side) is the
usual shape for a tool like this. Checked against it:

- **Trust:** strangers would give a solo project write access to their
  repos and let its agents run code from those repos on its servers.
- **Who pays:** a run costs about $0.30-0.50 on the API. Either I pay, or
  users bring keys and I store them. A Claude subscription can't be
  offered to others (Anthropic's terms).
- **Infrastructure:** today the sandbox is podman on one machine. Many
  tenants means isolating untrusted code per tenant, a queue, secrets per
  tenant: a product of its own before any agent work.
- **Buyer:** the companies I'd build for (EU, compliance) would rather run
  it on their own infrastructure with their own keys than send their code
  to a solo developer's service.
- **Timing:** every eval is still on planted bugs in a small repo.

For myself the same holds at work: employer code goes through the
employer's key on the employer's machines, not through mine.

## Decision

- **cofabriq is self-hosted.** It runs where the code is, with the
  operator's own keys. The path is local use, then a private deployment
  (network-level access such as Tailscale, no accounts in the app), then
  packaging so others can run it. No multi-tenant service.
- **One versioned artifact.** `cofabriq serve` (FastAPI) also serves the
  built UI, so API and UI versions always match; a Docker image later
  builds the UI in one stage and copies it into the Python image.
- **The UI lives in this repo as `ui/`**: a plain subfolder with its own
  `package.json` and lockfile, no workspace tooling and no
  frontend/backend restructure. Its types are generated from the API's
  OpenAPI schema; the API returns its own response models, not the
  internal ones, so internal changes stop at that layer.
- **Read-only first.** The UI only shows runs until the browser gate
  (0.5), which adds a start-up token, localhost binding and an origin
  check before any route can change something.

## Consequences

- No auth, billing or tenant isolation to build. Sharing means a demo
  (screenshots, a recorded run, a static export of playground runs), not
  sign-ups.
- The repo has two toolchains (uv and pnpm). `make check` covers the UI
  only when `ui/` changed, to keep the hook fast; CI later needs both.
- Dogfooding on this repo needs a target with both toolchains, or the
  loop pointed at the Python side first.
- The sandbox inside a container (for a deployment) is open: the host's
  podman socket or a rootless nested setup, decided by a spike when
  self-host packaging starts.

## Alternatives considered

- **Hosted SaaS:** rejected for now on trust, cost, infrastructure, buyer
  and timing (above). Revisit only if real users ask for it and the loop
  holds on real repos.
- **A separate UI repo** (client generated from the OpenAPI schema with
  orval): one toolchain per repo and a clean TypeScript target for
  dogfooding, but a version matrix between two artifacts, and every
  contract change in two PRs. Rejected because a self-hosted product ships
  as one artifact.
- **`frontend/` + `backend/` folders:** the same trade-offs as `ui/`, plus
  moving every Python path. Worth it only if the UI becomes half the
  product.
