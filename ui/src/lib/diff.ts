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
