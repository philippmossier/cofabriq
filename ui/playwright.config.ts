import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// The viewer on recorded data: `cofabriq serve`, read-only, over ui/e2e/fixtures/data
// (real runs, traces, specs and issues of the playground, copied from data/). No agent and
// no database: free, and run in CI. The live journey has its own config
// (playwright.journey.config.ts, `make journey`).

const root = path.resolve(import.meta.dirname, "..");
const data = path.join(import.meta.dirname, "e2e", "fixtures", "data");
const PORT = 8797;

export default defineConfig({
  testDir: "e2e",
  testMatch: "viewer.spec.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // the built UI (ui/dist): `make e2e` builds it first
    command: `uv run cofabriq serve --port ${PORT}`,
    cwd: root,
    url: `http://127.0.0.1:${PORT}/api/runs`,
    reuseExistingServer: !process.env.CI,
    env: {
      RUNS_DIR: path.join(data, "runs"),
      TRACE_DIR: path.join(data, "traces"),
      REFINE_DIR: path.join(data, "refine"),
      ISSUES_DIR: path.join(data, "issues"),
      TICKETS_DIR: path.join(data, "tickets"),
      CLONES_DIR: path.join(data, "clones"), // none: a snippet says so
      // the shipped playground target only (a link): no repo of the dev's own in targets/own
      TARGETS_DIR: path.join(import.meta.dirname, "e2e", "fixtures", "targets"),
    },
  },
});
