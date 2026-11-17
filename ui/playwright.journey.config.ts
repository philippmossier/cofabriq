import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// The live journey (`make journey`): the real flow through the real UI on the playground,
// headless. `cofabriq serve --write` on the real data/, Postgres and the agent: an issue is
// started under Issues, its plan (and test) approved, and every page saved to
// data/journey/<time>/ for review. Paid at list price (your plan pays), a few minutes.
// JOURNEY_REPO (the playground), JOURNEY_ISSUE (51, or a local ticket: L1) and JOURNEY_ROUTE
// (bug) pick what runs.

const root = path.resolve(import.meta.dirname, "..");
const PORT = 8798;
export const TOKEN_FILE = path.join(root, "data", "journey", ".token");

export default defineConfig({
  testDir: "e2e",
  testMatch: "journey.spec.ts",
  timeout: 25 * 60_000,
  workers: 1,
  reporter: "list",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: `http://127.0.0.1:${PORT}`,
    viewport: { width: 1400, height: 1000 },
    trace: "on",
  },
  webServer: {
    command: `uv run cofabriq serve --port ${PORT} --write --token-file ${TOKEN_FILE}`,
    cwd: root,
    url: `http://127.0.0.1:${PORT}/api/repos`,
    reuseExistingServer: false, // its token must be the one in the file
    timeout: 60_000,
  },
});
