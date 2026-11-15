import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";
import { TOKEN_FILE } from "../playwright.journey.config.ts";

// One issue through the whole journey in the browser: Start (or Start again) under Issues,
// the plan gate, the test gate on a feature, the end. Every page is saved as a screenshot
// and its text, with the API's view of the flow and the run, and the draft PR body, to
// data/journey/<time>/: what a person would review, readable without clicking.
// The agent's verdict varies run to run; what this asserts is that each page shows what it
// must. summary.json says how it ended.

const REPO = "philippmossier/local-ai-chat-playground";
const ISSUE = Number(process.env.JOURNEY_ISSUE ?? 51);
const ROUTE = process.env.JOURNEY_ROUTE ?? "bug";
const [OWNER, NAME] = REPO.split("/") as [string, string];
const OUT = path.resolve(
  import.meta.dirname,
  "..",
  "..",
  "data",
  "journey",
  `${new Date().toISOString().replace(/[-:]/g, "").slice(0, 15)}-${ISSUE}`,
);

let shot = 0;
async function save(page: Page, name: string) {
  shot += 1;
  const stem = path.join(OUT, `${String(shot).padStart(2, "0")}-${name}`);
  // open every fold, so the text holds what a reader could open
  await page.evaluate(() => document.querySelectorAll("details").forEach((d) => (d.open = true)));
  await page.screenshot({ path: `${stem}.png`, fullPage: true });
  writeFileSync(`${stem}.txt`, await page.locator("body").innerText());
}

async function api(page: Page, url: string, file: string): Promise<Record<string, unknown>> {
  const body = (await (await page.request.get(url)).json()) as Record<string, unknown>;
  writeFileSync(path.join(OUT, file), JSON.stringify(body, null, 2));
  return body;
}

const title = (page: Page) => page.locator('[data-slot="card-title"]').first();

/** The flow card's title once nothing runs: a gate's question or "Done: ...". */
async function settled(page: Page, minutes: number): Promise<string> {
  await expect(title(page)).toHaveText(/^(Approve the (plan|test)\?|Done: |Stopped mid-run)/, {
    timeout: minutes * 60_000,
  });
  return (await title(page).textContent()) ?? "";
}

/** Approve the gate on screen, then wait until the page has left it: right after the
 * click the card still shows the question, which `settled` would take for the answer. */
async function approve(page: Page, gate: "plan" | "test", minutes: number): Promise<string> {
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(title(page)).not.toHaveText(new RegExp(`^Approve the ${gate}\\?`), { timeout: 60_000 });
  return settled(page, minutes);
}

test(`#${ISSUE} (${ROUTE}): Start, the gates, the result`, async ({ page }) => {
  mkdirSync(OUT, { recursive: true });
  const started = Date.now();
  const token = readFileSync(TOKEN_FILE, "utf-8").trim();
  await page.goto(`/#token=${token}`);
  await expect(page.getByText("gate: you can approve")).toBeVisible();

  // Issues: Start, or Start again when the issue has a flow already
  await page.goto("/issues");
  const row = page.getByRole("row", { name: new RegExp(`#${ISSUE} `) });
  await row.locator("select").last().selectOption(ROUTE);
  await row.getByRole("button", { name: /^Start( again)?$/ }).click();
  await expect(page).toHaveURL(new RegExp(`/flows/${OWNER}/${NAME}/${ISSUE}$`));

  let state = await settled(page, 5);
  if (state.startsWith("Approve the plan")) {
    await save(page, "plan-gate");
    for (const part of ["The cause", "The change", "Check these"]) {
      await expect(page.getByRole("heading", { name: new RegExp(`^${part}`) })).toBeVisible();
    }
    await expect(page.getByText("the lines the change touches")).toBeVisible();
    await api(page, `/api/flows/${OWNER}/${NAME}/${ISSUE}`, "flow-at-plan-gate.json");
    state = await approve(page, "plan", 12);
  }
  if (state.startsWith("Approve the test")) {
    await save(page, "test-gate");
    await expect(page.getByRole("heading", { name: "The test (frozen once approved)" })).toBeVisible();
    state = await approve(page, "test", 12);
  }
  // the server stops with the test: the run must be over, not cut off
  expect(state).toMatch(/^Done: /);

  await save(page, "done");
  const flow = await api(page, `/api/flows/${OWNER}/${NAME}/${ISSUE}`, "flow.json");
  const record = String(flow.record ?? "");
  let pr = "";
  if (record) {
    await page.getByRole("link", { name: /The whole run/ }).click();
    await expect(page.getByRole("heading", { name: /^Diff/ })).toBeVisible();
    await save(page, "run");
    const run = await api(page, `/api/runs/${record}/${ISSUE}`, "run.json");
    pr = String(run.pr_body ?? "");
    if (pr) writeFileSync(path.join(OUT, "draft-pr.md"), pr);
  }
  if (flow.trace) await api(page, `/api/traces/${flow.trace}`, "steps.json");

  writeFileSync(
    path.join(OUT, "summary.json"),
    JSON.stringify(
      { issue: ISSUE, route: ROUTE, outcome: flow.outcome, record, minutes: (Date.now() - started) / 60_000, out: OUT },
      null,
      2,
    ),
  );
  console.log(`journey: #${ISSUE} ${String(flow.outcome)} -> ${OUT}`);
  if (pr) expect(pr).toContain(`Fixes #${ISSUE}`);
});
