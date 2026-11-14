# ADR-011: OpenTelemetry traces under the GenAI conventions, the backend as config

## Status

Accepted, 2026-11-14.

## Context

A run of the core loop takes minutes and touches three kinds of work: OpenAI
calls (triage, rerank, verdict), Claude Code sessions (refine, test writer,
builder, reviewer, proofs) and podman commands (install, repro, checks). The
run record in `data/runs/` says what came out and what it cost in total. It
doesn't say where the time went (a slow install or a slow agent), which agent
spent the budget, or what happened in a run that died halfway.

LangSmith would trace the LangGraph part out of the box (ADR-010 leaves it
off). It sees only what runs through LangChain, though: the agents are
subprocesses and the sandbox is podman, so both would stay invisible, and
the data would go to one vendor's SaaS. cofabriq is self-hosted (ADR-009).

## Decision

- **OpenTelemetry spans, exported over OTLP/HTTP** (`tracing.py`), off
  unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Without a provider the API is
  a no-op, so the instrumentation stays in the code at no cost.
- **Spans at the boundaries cofabriq owns**: one per OpenAI call (`chat`,
  `embeddings`), one per agent session (`invoke_agent claude-code`), one per
  sandbox command, one per graph node, a root per flow invocation and per
  triage. Hand-written, not auto-instrumented: the OpenAI calls go through
  one function, and no instrumentation library sees a subprocess.
- **The GenAI semantic conventions** (`gen_ai.operation.name`,
  `gen_ai.request.model`, `gen_ai.usage.*`, ...) where they name something,
  `cofabriq.*` for the rest (cost, turns, tools, sandbox command). The
  conventions are still in development, so their names live only in
  `tracing.py`.
- **Metadata only, never content.** No prompt, issue text, answer or diff
  goes into a span: issue text is untrusted and may carry secrets, and the
  conventions make content capture opt-in.
- **One trace per process, linked.** A flow paused at a gate resumes in
  another process, maybe hours later. The first trace's W3C `traceparent` is
  kept in the checkpoint (`FlowState.trace`), and each resume starts a new
  trace with a span link to it, the way an HTTP hop carries the same header.
- **The backend is config.** Jaeger runs in compose for local use; any OTLP
  backend works by endpoint and headers, a hosted one (Dynatrace, Honeycomb,
  Grafana) included.

## Consequences

- A run in Jaeger shows each node, agent session and sandbox command with
  its duration, model, cost and turns; a failed agent session is an error
  span that still carries what it cost.
- An agent session is one span: the model calls and tool uses inside Claude
  Code are not visible yet. That needs the agent's own event stream
  (`--output-format stream-json`) or its OpenTelemetry export joined to the
  parent trace.
- A worker thread starts with an empty context, so a span opened in one
  loses its parent. Triage runs its two calls in a copy of the caller's
  context; the evals' thread pools don't, so there each agent session is
  its own trace.
- With an endpoint set and the backend down, a command still finishes; the
  exporter logs its failed retries to stderr and gives up within seconds at
  exit.
- Three more direct dependencies (the OpenTelemetry API, SDK and OTLP/HTTP
  exporter, with protobuf).

## Alternatives considered

- **LangSmith**: traces the graph without code, but not the subprocess
  agents or the sandbox, and only to its own service.
- **Langfuse**: an LLM-specific UI (prompts, scores, cost per trace) over
  OpenTelemetry. Self-hosting it means Postgres, ClickHouse, Redis and
  object storage; it can still be pointed at later, by endpoint.
- **OTLP over gRPC**: the same data, but an extra native dependency
  (`grpcio`), and some hosted backends accept only HTTP.
- **Auto-instrumentation for OpenAI** (`opentelemetry-instrumentation-openai-v2`):
  patches the client, which cofabriq calls from one function anyway; the
  agents and the sandbox would still need hand-written spans, so one way
  for all three.
