import { createRootRoute, createRoute, createRouter, redirect } from "@tanstack/react-router";
import { Layout } from "./components/layout";
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

export const router = createRouter({
  routeTree: root.addChildren([index, specs, spec, runs, run]),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
