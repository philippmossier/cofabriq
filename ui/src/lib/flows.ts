import { Route } from "@/api/generated";

/** The route Start again suggests: the other one after a flow that went
 * wrong (the likeliest mistake is the type), the same after a fix. */
export function againRoute(flow: { route: Route; outcome: string }): Route {
  if (flow.outcome.startsWith("approved")) return flow.route;
  return flow.route === Route.feature ? Route.bug : Route.feature;
}
