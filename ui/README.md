# cofabriq UI

A read-only viewer over what refine and the loop stored: specs (criteria,
readiness, open questions) and runs (the test before any code, the change,
the review gate with its proofs, the draft PR body). It talks only to
`cofabriq serve`; nothing here changes anything (the browser gate comes in
0.5, ADR-009).

```bash
make ui                         # build into ui/dist
uv run cofabriq serve           # API + built UI on http://127.0.0.1:8765

# while working on the UI
uv run cofabriq serve &         # the API
cd ui && pnpm dev               # Vite on :5173, /api proxied to :8765
```

With `uv run cofabriq serve --write` the Flows page is the browser gate:
open the URL it prints (the token is in its `#fragment`, moved to
sessionStorage and out of the address bar), then approve or reject a
flow's plan or test. Flows are started with `cofabriq flow start <n>`.

The API client (`src/api/generated.ts`, TanStack Query hooks) is generated
by orval from `openapi.json`, which `cofabriq openapi` writes from the
API's response models. Never edit either by hand: `make check-ui`
regenerates both and fails when the committed ones are stale.

Stack: Vite, React, TanStack Router (code-based routes) and Query,
Tailwind, shadcn (`src/components/ui`, generated, not linted).
