import type { QueryClient } from "@tanstack/react-query";
import {
  type FlowSummary,
  Phase,
  Route,
  getFlowListQueryKey,
  getFlowQueryKey,
  getIssuesQueryKey,
} from "@/api/generated";

export const issueUrl = (repo: string, number: number) => `https://github.com/${repo}/issues/${number}`;

/** A flow's parts in order, as the stepper names them; `you`: a gate. */
export const PHASES: { phase: Phase; label: string; you?: boolean }[] = [
  { phase: Phase.spec, label: "Spec" },
  { phase: Phase.plan, label: "Plan", you: true },
  { phase: Phase.test, label: "Failing test" },
  { phase: Phase.test_gate, label: "Test", you: true },
  { phase: Phase.build, label: "Build" },
  { phase: Phase.review, label: "Review" },
  { phase: Phase.done, label: "Draft PR" },
];

/** How a part of the flow stands: `waits` for you at a gate, `now` runs,
 * `stopped` is where an ended flow stopped short. */
export type PhaseState = "done" | "now" | "waits" | "stopped" | "todo";

/** Ended well: a change that passed its test (and the review, if it ran). */
export function endedWell(outcome: string): boolean {
  return ["approved", "unproven", "green"].includes(outcome.split(":")[0]!);
}

export function phaseStates(f: FlowSummary): PhaseState[] {
  const at = PHASES.findIndex((p) => p.phase === f.phase);
  const ended = !f.running && !f.waiting;
  return PHASES.map((_, i) => {
    if (i < at) return "done";
    if (i > at) return "todo";
    if (f.waiting) return "waits";
    if (f.running) return "now";
    return ended && endedWell(f.outcome) ? "done" : "stopped";
  });
}

/** The route Start again suggests: the other one after a plan or spec that
 * went back (the likeliest mistake is the type), the same otherwise (a fix,
 * a run cut off or stopped later on). */
export function againRoute(flow: { route: Route; outcome: string }): Route {
  if (!/^(rejected|failed)/.test(flow.outcome)) return flow.route;
  return flow.route === Route.feature ? Route.bug : Route.feature;
}

/** After a start or an answer: the server already says "running", so ask
 * now instead of waiting up to a poll interval with a stale page. */
export async function refreshFlow(client: QueryClient, owner: string, name: string, number: number) {
  await Promise.all([
    client.invalidateQueries({ queryKey: getFlowQueryKey(owner, name, number) }),
    client.invalidateQueries({ queryKey: getFlowListQueryKey() }),
    client.invalidateQueries({ queryKey: getIssuesQueryKey(owner, name) }),
  ]);
}
