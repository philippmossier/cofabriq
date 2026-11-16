// A unified diff (`git diff`) as files, hunks and numbered lines.

export type DiffLine = { kind: "add" | "del" | "ctx"; text: string; old: number | null; new: number | null };
export type Hunk = { header: string; lines: DiffLine[] };
export type DiffFile = { path: string; isNew: boolean; isDeleted: boolean; added: number; removed: number; hunks: Hunk[] };

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/;

export function parseDiff(diff: string): DiffFile[] {
  const files: DiffFile[] = [];
  let file: DiffFile | undefined;
  let hunk: Hunk | undefined;
  let oldN = 0;
  let newN = 0;
  for (const line of diff.split("\n")) {
    const head = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
    if (head) {
      file = { path: head[2]!, isNew: false, isDeleted: false, added: 0, removed: 0, hunks: [] };
      files.push(file);
      hunk = undefined;
      continue;
    }
    if (!file) continue;
    if (line.startsWith("new file mode")) file.isNew = true;
    else if (line.startsWith("deleted file mode")) file.isDeleted = true;
    const h = line.match(HUNK);
    if (h) {
      oldN = Number(h[1]);
      newN = Number(h[2]);
      hunk = { header: line, lines: [] };
      file.hunks.push(hunk);
      continue;
    }
    if (!hunk || line.startsWith("\\")) continue; // "\ No newline at end of file"
    if (line.startsWith("+")) {
      hunk.lines.push({ kind: "add", text: line.slice(1), old: null, new: newN++ });
      file.added++;
    } else if (line.startsWith("-")) {
      hunk.lines.push({ kind: "del", text: line.slice(1), old: oldN++, new: null });
      file.removed++;
    } else if (line.startsWith(" ")) {
      hunk.lines.push({ kind: "ctx", text: line.slice(1), old: oldN++, new: newN++ });
    }
  }
  return files;
}

/** `old` -> `next` as diff lines, starting at line `from` of both: the
 * longest common subsequence stays context, the rest is removed or added
 * (a sketch is at most 30 lines a side, so the table is small). Lines
 * that differ only in trailing spaces count as the same. */
export function lineDiff(old: string[], next: string[], from: number): DiffLine[] {
  const same = (a: string, b: string) => a.trimEnd() === b.trimEnd();
  // lcs[i][j]: the common length of old[i..] and next[j..]
  const lcs = Array.from({ length: old.length + 1 }, () => new Array<number>(next.length + 1).fill(0));
  for (let i = old.length - 1; i >= 0; i--) {
    for (let j = next.length - 1; j >= 0; j--) {
      lcs[i]![j] = same(old[i]!, next[j]!) ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < old.length || j < next.length) {
    if (i < old.length && j < next.length && same(old[i]!, next[j]!)) {
      out.push({ kind: "ctx", text: next[j]!, old: from + i++, new: from + j++ });
    } else if (i < old.length && (j >= next.length || lcs[i + 1]![j]! >= lcs[i]![j + 1]!)) {
      out.push({ kind: "del", text: old[i]!, old: from + i++, new: null }); // removed before added
    } else {
      out.push({ kind: "add", text: next[j]!, old: null, new: from + j++ });
    }
  }
  return out;
}
