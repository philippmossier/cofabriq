import { createRootRoute, createRoute, createRouter, redirect } from "@tanstack/react-router";
import { Layout } from "./components/layout";
import { FlowPage, FlowsPage } from "./pages/flows";
import { IssuesPage } from "./pages/issues";
import { type EvalTab, evalTab } from "./lib/evals";
import { EvalsPage, RunPage, RunsPage } from "./pages/runs";
import { SpecPage } from "./pages/specs";

// Code-based routes: a handful of pages don't need the file-based router's codegen.

const root = createRootRoute({ component: Layout });

const index = createRoute({
  getParentRoute: () => root,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/issues" });
  },
});

const issues = createRoute({ getParentRoute: () => root, path: "/issues", component: IssuesPage });

const flows = createRoute({ getParentRoute: () => root, path: "/flows", component: FlowsPage });
const flow = createRoute({
  getParentRoute: () => root,
  path: "/flows/$owner/$name/$number",
  component: FlowPage,
});

const runs = createRoute({ getParentRoute: () => root, path: "/runs", component: RunsPage });
export const run = createRoute({
  getParentRoute: () => root,
  path: "/runs/$file/$key",
  component: RunPage,
});

// the tab is in the URL (?tab=gate): a run's "back" returns to its tab
const evals = createRoute({
  getParentRoute: () => root,
  path: "/evals",
  validateSearch: (search: Record<string, unknown>): { tab: EvalTab } => ({ tab: evalTab(search.tab) }),
  component: EvalsPage,
});

// old links to the specs list land on the Evals tab that holds it
const specs = createRoute({
  getParentRoute: () => root,
  path: "/specs",
  beforeLoad: () => {
    throw redirect({ to: "/evals", search: { tab: "specs" } });
  },
});
export const spec = createRoute({
  getParentRoute: () => root,
  path: "/specs/$file/$key",
  component: SpecPage,
});

export const router = createRouter({
  routeTree: root.addChildren([index, issues, flows, flow, runs, run, evals, specs, spec]),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
