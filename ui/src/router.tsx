import { createRootRoute, createRoute, createRouter, redirect } from "@tanstack/react-router";
import { Layout } from "./components/layout";
import { FlowPage, FlowsPage } from "./pages/flows";
import { RunPage, RunsPage } from "./pages/runs";
import { SpecPage, SpecsPage } from "./pages/specs";

// Code-based routes: five pages don't need the file-based router's codegen.

const root = createRootRoute({ component: Layout });

const index = createRoute({
  getParentRoute: () => root,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/specs" });
  },
});

const specs = createRoute({ getParentRoute: () => root, path: "/specs", component: SpecsPage });
export const spec = createRoute({
  getParentRoute: () => root,
  path: "/specs/$file/$key",
  component: SpecPage,
});
const runs = createRoute({ getParentRoute: () => root, path: "/runs", component: RunsPage });
export const run = createRoute({
  getParentRoute: () => root,
  path: "/runs/$file/$key",
  component: RunPage,
});

const flows = createRoute({ getParentRoute: () => root, path: "/flows", component: FlowsPage });
const flow = createRoute({
  getParentRoute: () => root,
  path: "/flows/$owner/$name/$number",
  component: FlowPage,
});

export const router = createRouter({
  routeTree: root.addChildren([index, specs, spec, runs, run, flows, flow]),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
