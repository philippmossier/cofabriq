import type { QueryClient } from "@tanstack/react-query";
import { Route, getFlowListQueryKey, getFlowQueryKey, getIssuesQueryKey } from "@/api/generated";

/** The route Start again suggests: the other one after a flow that went
 * wrong (the likeliest mistake is the type), the same after a fix. */
export function againRoute(flow: { route: Route; outcome: string }): Route {
  if (flow.outcome.startsWith("approved")) return flow.route;
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
