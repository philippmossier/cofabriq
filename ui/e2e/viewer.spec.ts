import { readFileSync } from "node:fs";
import path from "node:path";
import { type Page, expect, test } from "@playwright/test";
import type { RunStep, StepTree } from "../src/api/generated.ts";

// The viewer on recorded runs of the playground (e2e/fixtures/data): what each page shows.
// Fixtures: #51 (approved, a two-part run with a plan gate), #8 (approved with two open
// concerns, a new test file), a gate eval, two eval spec runs of 2026-11-15 (the later one
// with sketches), the issue store.

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

test("an issue opens in place, as text, before anything is paid for", async ({ page }) => {
  await page.goto("/issues");

  await page.getByRole("button", { name: /#51 Chat title/ }).click();

  await expect(page.getByTestId("issue-text")).toContainText("RAM and VRAM");
  await expect(page.getByText(/As last fetched .* Start reads it fresh/)).toBeVisible();
});

test("Runs lists the runs on issues; the benchmark runs are under Evals", async ({ page }) => {
  await page.goto("/runs");

  await expect(page.getByRole("link", { name: "#51" })).toBeVisible();
  await expect(page.getByText(/Chat title is cut off .* · philippmossier/)).toBeVisible();
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
  await expect(page.getByRole("link", { name: "copy-button-under-answer" })).toHaveCount(2); // two runs
  await page.goto(`/specs/${SPECS}/copy-button-under-answer`); // the one without sketches

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

test("a spec's sketch shows the lines as they are and as proposed, labelled not run", async ({ page }) => {
  // the eval specs of 2026-11-15 19:03: refine asked for sketches, kept when old is in the file
  await page.goto("/specs/eval-20261115T190323/title-ellipsis");

  const change = section(page, "The change");
  await expect(change.getByText(/proposed by the agent, not run/)).toBeVisible();
  const sketch = change.locator("pre", { hasText: "TITLE_LENGTH" }).first();
  await expect(sketch.locator(".bg-red-50")).toContainText("flat.length < TITLE_LENGTH");
  await expect(sketch.locator(".bg-emerald-50")).toContainText("flat.length <= TITLE_LENGTH");
  await expect(sketch).toContainText("52"); // where old starts, found by cofabriq
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

// A flow lives in Postgres, which this server doesn't have: its API answers are replayed
// from the journey of 2026-11-15 (e2e/fixtures/flows), its steps and run from the fixtures.
const FLOW = "/flows/philippmossier/local-ai-chat-playground/51";

async function replayFlow(page: Page, fixture: string, change: Record<string, unknown> = {}) {
  const flow = {
    ...JSON.parse(readFileSync(path.join(import.meta.dirname, "fixtures", "flows", fixture), "utf-8")),
    ...change,
  };
  await page.route("**/api/flows/philippmossier/local-ai-chat-playground/51", (route) =>
    route.fulfill({ json: flow }),
  );
  await page.route("**/api/flows", (route) => route.fulfill({ json: [flow] }));
}

const progress = (page: Page) => page.getByRole("list", { name: "progress" });

test("a plan gate: where the flow is, what to check, the issue, the answer at hand", async ({ page }) => {
  // a longer report than the recorded one: three lines, then "show more"
  const body = `I asked "What is the difference between RAM and VRAM?" and the title is cut off.\n\n${"More detail. ".repeat(60)}`;
  await replayFlow(page, "plan-gate.json", { body });
  await page.goto(FLOW);

  // the question is the answer bar's label; the spec's summary is in the header
  const bar = page.getByRole("region", { name: "your answer" });
  await expect(bar.getByTestId("flow-state")).toHaveText(/^Approve the plan\?/);
  await expect(
    page.getByText("size S · 1 file to change · tests in 1 file · proven by a unit test · 3 things to check"),
  ).toBeVisible();
  await expect(progress(page).locator('[aria-current="step"]')).toHaveText(/Plan/);
  const issue = section(page, "The issue, as reported");
  await expect(issue.getByRole("link", { name: "GitHub" })).toHaveAttribute("href", /\/issues\/51$/);
  const clamped = (await issue.getByTestId("issue-text").boundingBox())!.height;
  await issue.getByRole("button", { name: "show more" }).click();
  expect((await issue.getByTestId("issue-text").boundingBox())!.height).toBeGreaterThan(2 * clamped);
  await expect(issue.getByTestId("issue-text")).toContainText("More detail. More detail.");
  await expect(issue.getByRole("button", { name: "show less" })).toBeVisible();
  const cause = section(page, "The cause");
  // the cause leads in two sentences, the evidence is one click away
  await expect(cause.getByText(/^The chat title is made by/)).toBeVisible();
  await expect(cause.getByText(/how the agent found it \(\d+ sentences\)/)).toBeVisible();
  await expect(cause.getByText(/checks only 100-character input/)).toBeHidden();
  // the planned test reads as one: the cases that fail now, the ones that must stay
  const proof = section(page, "The proof: a test written before any code, which must fail first");
  const red = proof.getByRole("listitem").filter({ hasText: "keeps a title of exactly 42 characters whole" });
  await expect(red).toContainText("✗");
  await expect(red).toContainText("fails now");
  await expect(proof.getByText("passes now, must stay")).toHaveCount(2);
  await expect(proof.getByText(/Add boundary cases/)).toBeHidden(); // the writer's notes, folded
  // the steps so far are context at a gate: one line, the tree folded
  await expect(page.getByText(/^Steps so far: \d+ agent sessions · .* · \$\d/)).toBeVisible();
  await expect(page.getByText("Refine: read the code, write a spec")).toBeHidden();
  // waiting for you: in the nav and the tab's title
  await expect(page.getByRole("link", { name: /Flows\s*1/ })).toBeVisible();
  await expect(page).toHaveTitle("(1) waiting · cofabriq");

  // the answer stays on screen at the end of a long page; read-only without the link
  await page.mouse.wheel(0, 20_000);
  await expect(bar).toBeInViewport();
  await expect(bar.getByRole("button", { name: "Approve", exact: true })).toBeDisabled();
  await expect(bar.getByText("Read-only here")).toBeVisible();
});

test("a sketch marks only the lines it changes", async ({ page }) => {
  // a one-line insert into a block, as refine sketched it for #71 (old and new both whole)
  const plan = JSON.parse(readFileSync(path.join(import.meta.dirname, "fixtures", "flows", "plan-gate.json"), "utf-8"));
  const edit = {
    old: "    } finally {\n      writingChat.current = null;\n      inFlight.current = null;",
    new: "    } finally {\n      setGenerating(false);\n      writingChat.current = null;\n      inFlight.current = null;",
    line: 285,
  };
  await replayFlow(page, "plan-gate.json", { files: [{ ...plan.files[0], edits: [edit] }] });
  await page.goto(FLOW);

  const sketch = section(page, "The change").locator("pre").first();
  await expect(sketch.locator(".bg-emerald-50")).toHaveCount(1);
  await expect(sketch.locator(".bg-emerald-50")).toContainText("setGenerating(false);");
  await expect(sketch.locator(".bg-red-50")).toHaveCount(0);
  await expect(sketch).toContainText("writingChat.current = null;"); // as context
});

test("a test gate: how it fails first, the plan folded, Write it again needs a reason", async ({ page }) => {
  await replayFlow(page, "test-gate.json");
  const answers: unknown[] = [];
  await page.route("**/api/flows/philippmossier/local-ai-chat-playground/51/answer", (route) => {
    answers.push(route.request().postDataJSON());
    return route.fulfill({ json: { started: true } });
  });
  await page.goto("/#token=e2e"); // the gate's link: the token goes to sessionStorage
  await page.goto(FLOW);

  await expect(page.getByTestId("flow-state")).toHaveText(/^Approve the test\?/);
  await expect(progress(page).locator('[aria-current="step"]')).toHaveText(/Test/);
  await expect(section(page, "How it fails on the unchanged code")).toContainText(
    "keeps a text of exactly 42 characters whole",
  );
  // approved already: folded, one click away
  await expect(page.getByText("The plan you approved")).toBeVisible();
  await expect(page.getByRole("heading", { name: "The cause" })).toBeHidden();

  const bar = page.getByRole("region", { name: "your answer" });
  const again = bar.getByRole("button", { name: "Write it again (2 left)" });
  await expect(bar.getByRole("button", { name: "Approve and freeze" })).toBeEnabled();
  await expect(again).toBeDisabled();
  await bar.getByRole("textbox", { name: "reason" }).fill("test the 43-character case too");
  await again.click();
  await expect.poll(() => answers).toEqual([
    { approve: false, reason: "test the 43-character case too", again: true },
  ]);
});

test("while it works: the part it is in, and the step running now", async ({ page }) => {
  // the test gate approved, the build under way: its recorded steps with the last one open
  const flow = JSON.parse(readFileSync(path.join(import.meta.dirname, "fixtures", "flows", "done.json"), "utf-8"));
  Object.assign(flow, { running: true, phase: "build", outcome: "tested", record: "" });
  await page.route("**/api/flows/philippmossier/local-ai-chat-playground/51", (r) => r.fulfill({ json: flow }));
  await page.route("**/api/flows", (r) => r.fulfill({ json: [flow] }));
  await page.route(`**/api/traces/${flow.trace}`, async (route) => {
    const res = await route.fetch();
    const tree = (await res.json()) as StepTree;
    const open = (s: RunStep | undefined): RunStep => Object.assign(s!, { status: "running", ms: null });
    const root = open(tree.steps.at(-1));
    const build = open(root.children.find((c) => c.title.startsWith("Build")));
    open(build.children[0]);
    await route.fulfill({ response: res, json: tree });
  });
  await page.goto(FLOW);

  await expect(page.getByTestId("flow-state")).toHaveText(/^Building the fix against your test/);
  await expect(progress(page).locator('[aria-current="step"]')).toHaveText(/Build/);
  await expect(page.getByText(/^Now: Build: fix the code against the test · Agent session: claude-/)).toBeVisible();
  await expect(page.getByRole("region", { name: "your answer" })).toHaveCount(0);
});

test("a finished flow: the result first, one cost, the PR body a click away", async ({ page }) => {
  await replayFlow(page, "done.json");
  await page.goto(FLOW);

  await expect(page.getByTestId("flow-state")).toHaveText("Done: approved");
  await expect(progress(page).getByText("Draft PR")).toBeVisible();
  await expect(progress(page).locator('[aria-current="step"]')).toHaveCount(0);
  const card = page.locator('[data-slot="card"]', { hasText: "Fixed:" }).last();
  await expect(card).toContainText(/list price for the whole run: \d+ agent sessions/);
  await expect(page.getByRole("region", { name: "your answer" })).toHaveCount(0);

  await card.getByRole("link", { name: "the draft PR body" }).click();
  await expect(page).toHaveURL(new RegExp(`/runs/${R51}/51#draft-pr$`));
  await expect(page.getByRole("heading", { name: /^#51 Chat title is cut off/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy", exact: true })).toBeVisible();
});
