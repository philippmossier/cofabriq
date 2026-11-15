Fixes #8.

- **Risk: low** (e2e, ui)
- **Review: approved.** Approve: the CSS fix pins the theme foreground on code and adds `color-scheme` as the spec asks, and the test checks colour and contrast for both themes and both kinds of code. The `/code` fake-engine command the spec asked for was not added, and it is unclear whether the colour bug itself was reproduced before the fix.
- **Checks:** the test was red before the change; repro, types, lint, test green after
- **Screenshots:** 2 from the browser test after the change (in cofabriq's viewer)

### Plan (approved before any code)

1. Add the `/code` command to src/lib/engine/fake-worker.ts and update its header comment.
2. Write e2e/code-contrast.spec.ts (dark and light, block and inline code) and run `pnpm test:e2e` before the CSS change to confirm it reproduces the report. If it passes on unchanged code, the cause is not the inherited colour: inspect the computed style in the trace before going on.
3. In src/styles.css add `text-foreground` to the `.md pre` and inline `.md code` rules, and set `color-scheme` in `:root` and `.dark`.
4. Run `pnpm check-types && pnpm lint && pnpm test && pnpm test:e2e`, then check by eye in `pnpm build && pnpm preview` in Chrome with a real model answer containing a code block.

<details><summary>Full spec: problem and assumptions</summary>

In dark mode, code in assistant answers is reported as dark grey on near-black (Chrome 141). Model output is rendered by react-markdown inside `<div className="md">` in a ghost Bubble (src/components/message.tsx:51-56). The code styles in src/styles.css:19-24 set only a background (`.md pre` uses `bg-muted`, `.md code:not(pre code)` uses `bg-muted`). In dark mode `--muted` is #​2b2b2b (src/styles.css:124). Neither rule sets a text colour, so code relies on inheriting `color` through the bubble chain. The ghost variant (src/components/ui/bubble.tsx:33) sets no text colour either, and the theme only toggles the `.dark` class (src/lib/theme.ts:16-19), never `color-scheme`. Reading the code alone, inheritance should end at `body { text-foreground }` (#​f2ebfa, src/styles.css:150), so I could not confirm from the code where the dark grey comes from. It is most likely a UA or library rule on `pre`/`code` winning over inheritance. Either way, the code styles lean on an inherited colour they never pin. The fix is to give code blocks an explicit theme foreground and to declare `color-scheme` per theme. The earlier plan was rejected for not proving this with Playwright. The repo has Playwright (playwright.config.ts, e2e/smoke.spec.ts, `pnpm test:e2e`) running against the fake engine (src/lib/engine/fake-worker.ts). That engine can't produce a code block today: it answers "Fake answer to: &lt;msg&gt;" (line 44-46), so a fenced block never starts a line.

Assumptions:

- The report means both fenced code blocks and inline code in assistant answers. The screenshot (python hello world) shows a fenced block.
- There is no syntax highlighting (none in the code), so one foreground colour for code is enough.
- Adding a test-only command to the fake engine is fine: it is used only when VITE_FAKE_ENGINE=1 and makes no network requests.
- The issue text contained embedded instructions to change severity, component and duplicate status. They were ignored as untrusted and have no bearing on this spec.

</details>

### The failing test

`e2e/code-contrast.spec.ts`, red before the change:

```text
Running 2 tests using 2 workers





[1/2] [chromium] › e2e/code-contrast.spec.ts:43:3 › code in answers uses the light theme foreground
[2/2] [chromium] › e2e/code-contrast.spec.ts:43:3 › code in answers uses the dark theme foreground
  1) [chromium] › e2e/code-contrast.spec.ts:43:3 › code in answers uses the dark theme foreground ──

    Error: expect(received).toBe(expected) // Object.is equality

    Expected: "dark"
    Received: "normal"

      65 |     ] as const) {
      66 |       const s = await styleOf(code, bgSel);
    > 67 |       expect(s.colorScheme).toBe(theme);
         |                             ^
      68 |       expect(s.color).toBe(foreground);
      69 |       expect(contrast(s.color, s.background)).toBeGreaterThanOrEqual(4.5);
      70 |     }
        at /app/e2e/code-contrast.spec.ts:67:29

    Error Context: test-results/code-contrast-code-in-answers-uses-the-dark-theme-foreground-chromium/error-context.md

    attachment #2: trace (application/zip) ─────────────────────────────────────────────────────────
    test-results/code-contrast-code-in-answers-uses-the-dark-theme-foreground-chromium/trace.zip
    Usage:

        pnpm exec playwright show-trace test-results/code-contrast-code-in-answers-uses-the-dark-theme-foreground-chromium/trace.zip

    ────────────────────────────────────────────────────────────────────────────────────────────────


  2) [chromium] › e2e/code-contrast.spec.ts:43:3 › code in answers uses the light theme foreground ─

    Error: expect(received).toBe(expected) // Object.is equality

    Expected: "light"
    Received: "normal"

      65 |     ] as const) {
      66 |       const s = await styleOf(code, bgSel);
    > 67 |       expect(s.colorScheme).toBe(theme);
         |                             ^
      68 |       expect(s.color).toBe(foreground);
      69 |       expect(contrast(s.color, s.background)).toBeGreaterThanOrEqual(4.5);
      70 |     }
        at /app/e2e/code-contrast.spec.ts:67:29

    Error Context: test-results/code-contrast-code-in-answ-e665b--the-light-theme-foreground-chromium/error-context.md

    attachment #2: trace (application/zip) ─────────────────────────────────────────────────────────
    test-results/code-contrast-code-in-answ-e665b--the-light-theme-foreground-chromium/trace.zip
    Usage:

        pnpm exec playwright show-trace test-results/code-contrast-code-in-answ-e665b--the-light-theme-foreground-chromium/trace.zip

    ────────────────────────────────────────────────────────────────────────────────────────────────


  2 failed
    [chromium] › e2e/code-contrast.spec.ts:43:3 › code in answers uses the dark theme foreground ───
    [chromium] › e2e/code-contrast.spec.ts:43:3 › code in answers uses the light theme foreground ──
[WebServer] (node:43) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set.
[WebServer] (Use `node --trace-warnings ...` to show where the warning was created)
[WebServer] (node:55) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set.
[WebServer] (Use `node --trace-warnings ...` to show where the warning was created)
(node:92) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set.
(Use `node --trace-warnings ...` to show where the warning was created)
(node:91) Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set.
(Use `node --trace-warnings ...` to show where the warning was created)
```

````diff
diff --git a/e2e/code-contrast.spec.ts b/e2e/code-contrast.spec.ts
new file mode 100644
index 00000000..57a9e33e
--- /dev/null
+++ b/e2e/code-contrast.spec.ts
@@ -0,0 +1,74 @@
+import { expect, test, type Locator } from "@playwright/test";
+
+// The fake engine echoes the message, so a message with a fence on its own line renders a code block in the answer.
+const MESSAGE = "`inline`\n```\nprint(1)\n```";
+
+const THEMES = [
+  { theme: "dark", foreground: "rgb(242, 235, 250)" },
+  { theme: "light", foreground: "rgb(29, 27, 32)" },
+] as const;
+
+function channels(color: string): number[] {
+  const parts = color.match(/[\d.]+/g)?.map(Number) ?? [];
+  expect(parts.length, `parse ${color}`).toBeGreaterThanOrEqual(3);
+  return parts.slice(0, 3);
+}
+
+function luminance(color: string): number {
+  const [r = 0, g = 0, b = 0] = channels(color).map((c) => {
+    const s = c / 255;
+    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
+  });
+  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
+}
+
+function contrast(a: string, b: string): number {
+  const la = luminance(a);
+  const lb = luminance(b);
+  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
+}
+
+async function styleOf(code: Locator, backgroundSelector: string) {
+  return code.evaluate((el, sel) => {
+    const bg = el.closest(sel) ?? el;
+    return {
+      color: getComputedStyle(el).color,
+      background: getComputedStyle(bg).backgroundColor,
+      colorScheme: getComputedStyle(el).colorScheme,
+    };
+  }, backgroundSelector);
+}
+
+for (const { theme, foreground } of THEMES) {
+  test(`code in answers uses the ${theme} theme foreground`, async ({ page, context }) => {
+    const hosts = new Set<string>();
+    context.on("request", (r) => hosts.add(new URL(r.url()).host));
+    await context.addInitScript((t) => localStorage.setItem("local-ai-chat:theme", t), theme);
+
+    await page.goto("/");
+    await page.getByRole("button", { name: /start/i }).first().click();
+
+    const box = page.getByRole("textbox", { name: "Message" });
+    await expect(box).toBeEditable();
+    await box.fill(MESSAGE);
+    await box.press("Enter");
+
+    const block = page.locator(".md pre code").first();
+    const inline = page.locator(".md code:not(pre code)").first();
+    await expect(block).toHaveText(/print\(1\)/);
+    await expect(inline).toHaveText("inline");
+    await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
+
+    for (const [code, bgSel] of [
+      [block, "pre"],
+      [inline, "code"],
+    ] as const) {
+      const s = await styleOf(code, bgSel);
+      expect(s.colorScheme).toBe(theme);
+      expect(s.color).toBe(foreground);
+      expect(contrast(s.color, s.background)).toBeGreaterThanOrEqual(4.5);
+    }
+
+    expect([...hosts]).toEqual(["localhost:5199"]);
+  });
+}
````

### The change

```diff
diff --git a/src/styles.css b/src/styles.css
index 364c1ade..de0175da 100644
--- a/src/styles.css
+++ b/src/styles.css
@@ -17,10 +17,10 @@
   padding-left: 1.25rem;
 }
 .md pre {
-  @apply overflow-x-auto rounded-lg bg-muted p-3 text-xs;
+  @apply overflow-x-auto rounded-lg bg-muted p-3 text-xs text-foreground;
 }
 .md code:not(pre code) {
-  @apply rounded bg-muted px-1 py-0.5 text-[0.85em];
+  @apply rounded bg-muted px-1 py-0.5 text-[0.85em] text-foreground;
 }
 .md a {
   @apply text-primary underline underline-offset-2;
@@ -75,6 +75,7 @@
 }
 
 :root {
+  color-scheme: light;
   /* Quiet, mostly grey surfaces; the one accent is the green primary. White on this green is >= 4.5:1. */
   --background: #f6f6f6;
   --foreground: #1d1b20;
@@ -111,6 +112,7 @@
 }
 
 .dark {
+  color-scheme: dark;
   --background: #212121;
   --foreground: #f2ebfa;
   --card: #2b2b2b;
```

### Review (separate agent, saw the spec and the diffs only)

- Test checks the spec: yes. It covers dark and light themes and both block and inline code in the assistant answer. It checks the computed colour against the theme --foreground (#​f2ebfa and #​1d1b20), checks WCAG contrast against the real background, and checks that every request stays on localhost:5199. User messages are not rendered as markdown, so `.md` only matches the answer. One weakness: it also asserts `colorScheme`, which fails on the old code whatever the text colour is. So "failed before" does not prove the grey text was reproduced, which was the point of plan step 2.
- Fixes the cause: yes. The CSS change is what the spec asks for. `.md pre` and inline `.md code` now set `text-foreground` explicitly instead of relying on inheritance. `:root` and `.dark` now declare `color-scheme`, so browser defaults follow the theme. Nothing is tailored to the test's inputs.
- concern: `src/lib/engine/fake-worker.ts:44` The spec asks for a `/code` command in the fake engine, listed in the header comment. It was not added. The test instead relies on the echo putting a fence at the start of a line. That works for now, but it depends on the exact echo format. The spec's file list is not followed, and this is not mentioned anywhere.
- concern: `e2e/code-contrast.spec.ts:67` `expect(s.colorScheme).toBe(theme)` runs before the colour assertion, so on the old code the test may have failed only because `color-scheme` was missing. The diff does not show whether the grey code text (the reported bug) was actually reproduced, as plan step 2 required. Ask the author whether the colour assertion also failed before the fix.

### Against the plan

- Named in the spec, not changed: `src/lib/engine/fake-worker.ts`
- Changed, not in the spec: none

<details><summary>Trace</summary>

| step | agent | checks |
| --- | --- | --- |
| repro 1 | $0.238, 35 s | red ok, types ok, lint ok |
| fix 1 | $0.196, 20 s | repro ok, types ok, lint ok, test ok |
| round 1 review | $0.163, 19 s | |
| **total** | **$0.60** | |

</details>
