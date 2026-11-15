import { type Page, expect, test } from "@playwright/test";

// The viewer on recorded runs of the playground (e2e/fixtures/data): what each page shows.
// Fixtures: #51 (approved, a two-part run with a plan gate), #8 (approved with two open
// concerns, a new test file), a gate eval, the eval specs of 2026-11-15, the issue store.

const R51 = "philippmossier-local-ai-chat-playground-51-20261115T133809";
const R8 = "philippmossier-local-ai-chat-playground-8-20261115T155711";
const SPECS = "eval-20261115T152304";

const section = (page: Page, title: string) =>
  page.locator("section", { has: page.getByRole("heading", { name: title, exact: true }) });

test("the viewer opens on Issues, read-only without the gate's link", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveURL(/\/issues$/);
  await expect(page.getByText("philippmossier/local-ai-chat-playground")).toBeVisible();
  await expect(page.getByText("Read-only here")).toBeVisible();
  const row = page.getByRole("row", { name: /#51 Chat title/ });
  await expect(row.getByRole("button", { name: "Start" })).toBeDisabled();
});

test("Runs lists the runs on issues; the benchmark runs are under Evals", async ({ page }) => {
  await page.goto("/runs");

  await expect(page.getByRole("link", { name: "#51" })).toBeVisible();
  await expect(page.getByRole("link", { name: "#8" })).toBeVisible();
  await expect(page.getByText("reload-active-chat-right")).toHaveCount(0);

  await page.getByRole("link", { name: "Evals" }).click();
  await page.getByRole("tab", { name: "gate" }).click();
  await expect(page).toHaveURL(/\/evals\?tab=gate$/);
  await expect(page.getByRole("link", { name: "reload-active-chat-right" })).toBeVisible();
});

test("an approval with open concerns leads with them", async ({ page }) => {
  await page.goto(`/runs/${R8}/8`);

  const card = page.locator('[data-slot="card"]', { hasText: "open concerns" });
  await expect(card.locator('[data-slot="card-title"]')).toHaveText(
    "Approved, with 2 open concerns: read them before you trust the fix.",
  );
  await expect(card.getByText("src/lib/engine/fake-worker.ts:44")).toBeVisible();
  // no answer key on a real issue: no judge verdicts in the header
  await expect(page.getByText("judge", { exact: true })).toHaveCount(0);
});

test("the diff is per file, with line numbers and a new file marked", async ({ page }) => {
  await page.goto(`/runs/${R8}/8`);

  const test = page.locator("details", { hasText: "e2e/code-contrast.spec.ts" });
  await expect(test.getByText("new file")).toBeVisible();
  const css = page.locator("details", { hasText: "src/styles.css" }).last();
  await expect(css).toContainText("text-foreground");
  await expect(css.locator(".bg-red-50")).toHaveCount(2); // two lines replaced
  await expect(css.locator(".bg-emerald-50")).toHaveCount(4);
});

test("the step tree: two parts of the run, the wait at the gate, four levels", async ({ page }) => {
  await page.goto(`/runs/${R51}/51`);

  await expect(page.getByText("Run the flow")).toHaveCount(2);
  await expect(page.getByText(/waited .* for your answer at the gate/)).toBeVisible();

  await page.getByRole("tab", { name: "+ Data" }).click();
  await expect(page.getByText(/tokens: .* in/).first()).toBeHidden(); // a run before cache tokens
  await page.getByRole("button", { name: /what it got and gave/ }).first().click();
  await expect(page.getByText("update:").first()).toBeVisible();

  await page.getByRole("tab", { name: "+ Code" }).click();
  await expect(page.getByRole("link", { name: /build_stage · run\.py:\d+/ })).toBeVisible();

  await page.getByRole("tab", { name: "+ Why" }).click();
  await expect(page.getByText(/read-only agent/i).first()).toBeVisible();
});

test("a spec reads as a review: cause, change, proof, what to check", async ({ page }) => {
  await page.goto(`/evals?tab=specs`);
  await page.getByRole("link", { name: "copy-button-under-answer" }).click();

  await expect(section(page, "The cause")).toBeVisible();
  const change = section(page, "The change");
  await expect(change.getByText("the lines the change touches")).toBeVisible();
  await expect(change.getByRole("link", { name: /src\/components\/message\.tsx/ })).toBeVisible();
  // one file: its snippet opens by itself; the fixtures have no clone, and it says so
  await expect(change.getByText("Not in the local clone.").first()).toBeVisible();
  await expect(section(page, "Check these: where the agent guessed, what it couldn't answer")).toBeVisible();

  await page.getByRole("link", { name: "← Evals" }).click();
  await expect(page).toHaveURL(/\/evals\?tab=specs$/);
});

test("in the agent's text a `span` is code and a fence stays as written", async ({ page }) => {
  // the spec as served, its problem replaced by text with both (regression: e5431b1)
  await page.route(`**/api/specs/${SPECS}/copy-button-under-answer`, async (route) => {
    const res = await route.fetch();
    const spec = await res.json();
    spec.problem = "Send x\n```\nprint(1)\n```\nand `inline` code.";
    await route.fulfill({ response: res, json: spec });
  });
  await page.goto(`/specs/${SPECS}/copy-button-under-answer`);

  const cause = section(page, "The cause");
  await expect(cause.locator("code", { hasText: "inline" })).toBeVisible();
  await expect(cause).toContainText("```");
  await expect(cause.locator("code", { hasText: "print(1)" })).toHaveCount(0);
});
