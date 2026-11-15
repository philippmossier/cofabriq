Fixes #51.

- **Risk: low** (chat)
- **Review: approved.** I approve: the one-character guard change fixes the off-by-one the spec describes, and the new tests check both the 42-character and 43-character cases, including whitespace collapsing.
- **Checks:** the test was red before the change; repro, types, lint, test green after

### Plan (approved before any code)

1. In src/lib/chat/store.ts:52 change `flat.length < TITLE_LENGTH` to `flat.length <= TITLE_LENGTH`.
2. Add boundary tests in src/lib/chat/chat.test.ts for 42 characters (unchanged) and 43 characters (truncated to 42, ending in "…").
3. Run pnpm check-types &amp;&amp; pnpm lint &amp;&amp; pnpm test.

<details><summary>Full spec: problem and assumptions</summary>

Off-by-one in `titleFrom` at src/lib/chat/store.ts:52. The guard is `flat.length < TITLE_LENGTH` (TITLE_LENGTH = 42, line 48), so a message that is exactly 42 characters long after whitespace is collapsed still goes to the truncation branch. That branch returns `flat.slice(0, 41) + "…"`, which is also 42 characters. The title ends up no shorter than the original, and the last character (here probably the "?") is swapped for "…". Longer questions are cut correctly, which matches the report. Titles of 42 characters or fewer should be shown unchanged. The sidebar's CSS `truncate` at src/components/chat-sidebar.tsx:88 is a separate, visual-only ellipsis that appears only when the text overflows the sidebar width. It is not the cause.

Assumptions:

- The reporter's question plus extra words came to exactly 42 characters after whitespace was collapsed. They say "nothing is missing", but in fact the final character, probably "?", was replaced by "…".
- Titles already stored in localStorage keep their old value. A title is set only once, when the chat is created (store.ts:110), so the fix applies to new chats only.
- TITLE_LENGTH = 42 stays the intended maximum length, counting the ellipsis.

</details>

### The failing test

`src/lib/chat/chat.test.ts`, red before the change:

```text
RUN  v5.0.3 /app

 ❯ src/lib/chat/chat.test.ts (22 tests | 1 failed) 10ms
   ✓ chatReducer (7)
     ✓ creates a chat and makes it active 1ms
     ✓ adds the user message and an empty assistant placeholder, and titles the chat once 0ms
     ✓ streams tokens into the assistant message and records stats 0ms
     ✓ deleting the active chat selects another one 0ms
     ✓ keeps updatedAt current so the history orders by last use 0ms
     ✓ clear wipes everything 0ms
     ✓ selecting an unknown chat is ignored 0ms
   ❯ titleFrom (3)
     ✓ flattens whitespace and truncates with an ellipsis 1ms
     × keeps a title of exactly 42 characters whole 4ms
     ✓ cuts a 43 character title to 42 ending in an ellipsis 0ms
   ✓ persistence (8)
     ✓ round-trips 0ms
     ✓ falls back to an empty state for null 0ms
     ✓ falls back to an empty state for "" 0ms
     ✓ falls back to an empty state for "not json" 1ms
     ✓ falls back to an empty state for "[]" 0ms
     ✓ falls back to an empty state for "{\"chats\":\"nope\"}" 0ms
     ✓ falls back to an empty state for "{\"chats\":[{\"id\":1}]}" 0ms
     ✓ drops malformed chats but keeps valid ones 0ms
   ✓ chats saved before updatedAt existed (1)
     ✓ fall back to createdAt instead of being dropped 0ms
   ✓ toModelMessages (1)
     ✓ leaves out empty placeholders and failed replies 0ms
   ✓ sync from another tab (2)
     ✓ takes the other tab's chats instead of overwriting them later 0ms
     ✓ keeps the chat this tab is still writing into 0ms

 Test Files  1 failed (1)
      Tests  1 failed | 21 passed (22)
   Start at  13:37:32
   Duration  162ms (transform 50%, import 20%, tests 17%, worker 13%)


⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  src/lib/chat/chat.test.ts > titleFrom > keeps a title of exactly 42 characters whole
AssertionError: expected 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx…' to be 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx…' // Object.is equality

Expected: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx?"
Received: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx…"

 ❯ src/lib/chat/chat.test.ts:111:30
    109|     const exact = `${"x".repeat(41)}?`;
    110|     expect(exact).toHaveLength(42);
    111|     expect(titleFrom(exact)).toBe(exact);
       |                              ^
    112|     expect(titleFrom(`  ${"x".repeat(20)}\n\n${"y".repeat(20)}? `)).to…
    113|       `${"x".repeat(20)} ${"y".repeat(20)}?`,

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯
```

```diff
diff --git a/src/lib/chat/chat.test.ts b/src/lib/chat/chat.test.ts
index 41cf7e9c..9a17a2b7 100644
--- a/src/lib/chat/chat.test.ts
+++ b/src/lib/chat/chat.test.ts
@@ -104,6 +104,21 @@ describe("titleFrom", () => {
     expect(titleFrom(long)).toHaveLength(42);
     expect(titleFrom(long).endsWith("…")).toBe(true);
   });
+
+  it("keeps a title of exactly 42 characters whole", () => {
+    const exact = `${"x".repeat(41)}?`;
+    expect(exact).toHaveLength(42);
+    expect(titleFrom(exact)).toBe(exact);
+    expect(titleFrom(`  ${"x".repeat(20)}\n\n${"y".repeat(20)}? `)).toBe(
+      `${"x".repeat(20)} ${"y".repeat(20)}?`,
+    );
+  });
+
+  it("cuts a 43 character title to 42 ending in an ellipsis", () => {
+    const over = "x".repeat(43);
+    expect(titleFrom(over)).toBe(`${"x".repeat(41)}…`);
+    expect(titleFrom(over)).toHaveLength(42);
+  });
 });
 
 describe("persistence", () => {
```

### The change

```diff
diff --git a/src/lib/chat/store.ts b/src/lib/chat/store.ts
index 8a1f17c4..501daaf4 100644
--- a/src/lib/chat/store.ts
+++ b/src/lib/chat/store.ts
@@ -49,7 +49,7 @@ const TITLE_LENGTH = 42;
 
 export function titleFrom(text: string): string {
   const flat = text.replace(/\s+/g, " ").trim();
-  return flat.length < TITLE_LENGTH ? flat : `${flat.slice(0, TITLE_LENGTH - 1)}…`;
+  return flat.length <= TITLE_LENGTH ? flat : `${flat.slice(0, TITLE_LENGTH - 1)}…`;
 }
 
 const mapChat = (state: ChatState, id: string, fn: (c: Chat) => Chat): ChatState => ({
```

### Review (separate agent, saw the spec and the diffs only)

- Test checks the spec: yes. The new tests cover both sides of the boundary. A 42-character input, including one that is 42 only after whitespace is collapsed, comes back unchanged. A 43-character input is cut to exactly 42 characters ending in "…". The existing 100-character case is kept. The 42-character case would fail under the old `<` guard.
- Fixes the cause: yes. The guard changes from `<` to `<=` TITLE_LENGTH, as the spec asks. Any title of 42 characters or fewer is now returned unchanged, and longer ones are still cut to 42 including the ellipsis. Nothing depends on the test's specific inputs, and no other place in src uses the same truncation pattern.

### Against the plan

- Named in the spec, not changed: none
- Changed, not in the spec: none

<details><summary>Trace</summary>

| step | agent | checks |
| --- | --- | --- |
| repro 1 | $0.117, 12 s | red ok, types ok, lint ok |
| fix 1 | $0.098, 7 s | repro ok, types ok, lint ok, test ok |
| round 1 review | $0.093, 9 s | |
| **total** | **$0.31** | |

</details>
