import { Highlight, type Language, themes } from "prism-react-renderer";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { type DiffFile, parseDiff } from "@/lib/diff";
import { cn } from "@/lib/utils";

const LANGUAGES: Record<string, Language> = {
  ts: "typescript",
  mts: "typescript",
  tsx: "tsx",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "jsx",
  json: "json",
  css: "css",
  py: "python",
  yml: "yaml",
  yaml: "yaml",
  md: "markdown",
  html: "markup",
  go: "go",
  sql: "sql",
};

function languageOf(path: string): Language | undefined {
  return LANGUAGES[path.split(".").at(-1)?.toLowerCase() ?? ""];
}

/** Lines of the target's code, highlighted by its file type. The target's
 * code is untrusted: tokens become React text, never HTML. `row` wraps each
 * line (numbers, markers, a background). */
export function Highlighted({
  path,
  lines,
  row,
}: {
  path: string;
  lines: string[];
  row: (i: number, code: ReactNode) => ReactNode;
}) {
  const language = languageOf(path);
  if (!language) return <>{lines.map((text, i) => row(i, text))}</>;
  return (
    <Highlight code={lines.join("\n")} language={language} theme={themes.github}>
      {({ tokens, getTokenProps }) => (
        <>
          {tokens.map((line, i) =>
            row(
              i,
              line.map((token, k) => {
                const { key: _, ...props } = getTokenProps({ token, key: k });
                return <span key={k} {...props} />;
              }),
            ),
          )}
        </>
      )}
    </Highlight>
  );
}

const NUM = "w-10 shrink-0 pr-3 text-right text-muted-foreground select-none";

/** A file's lines from `start`, numbered; `mark` colours some of them. */
export function CodeLines({
  path,
  start,
  lines,
  mark,
}: {
  path: string;
  start: number;
  lines: string[];
  mark?: (n: number) => string | undefined;
}) {
  return (
    <pre className="mt-1 max-h-96 overflow-auto rounded-md border bg-white py-2 font-mono text-xs leading-relaxed">
      <Highlighted
        path={path}
        lines={lines}
        row={(i, code) => (
          <div key={i} className={cn("flex px-3", mark?.(start + i))}>
            <span className={NUM}>{start + i}</span>
            <span className="whitespace-pre">{code}</span>
          </div>
        )}
      />
    </pre>
  );
}

/** One place of a change as the agent proposes it at the plan gate: its lines
 * as they are (found in the file at `line`) and what they become. Not run. */
export function ProposedEdit({ path, line, old, next }: { path: string; line: number; old: string; next: string }) {
  const rows = [
    ...old.split("\n").map((text, i) => ({ kind: "del" as const, text, n: line + i })),
    ...(next ? next.split("\n") : []).map((text, i) => ({ kind: "add" as const, text, n: line + i })),
  ];
  return (
    <pre className="overflow-auto rounded-md border bg-white py-1 font-mono text-xs leading-relaxed">
      <Highlighted
        path={path}
        lines={rows.map((r) => r.text)}
        row={(i, code) => {
          const r = rows[i]!;
          return (
            <div key={i} className={cn("flex px-1", ROW[r.kind])}>
              <span className={cn(NUM, "w-9 pr-2")}>{r.n}</span>
              <span className="w-4 shrink-0 text-muted-foreground select-none">{SIGN[r.kind]}</span>
              <span className="whitespace-pre">{code}</span>
            </div>
          );
        }}
      />
    </pre>
  );
}

const ROW = { add: "bg-emerald-50", del: "bg-red-50", ctx: "" };
const SIGN = { add: "+", del: "−", ctx: " " };

function FileDiff({ file, open }: { file: DiffFile; open: boolean }) {
  return (
    <details open={open} className="rounded-md border">
      <summary className="flex items-center gap-2 px-3 py-1.5 text-sm">
        <code className="font-medium">{file.path}</code>
        {file.isNew && <Badge variant="outline">new file</Badge>}
        {file.isDeleted && <Badge variant="outline">deleted</Badge>}
        <span className="text-emerald-700">+{file.added}</span>
        <span className="text-destructive">−{file.removed}</span>
      </summary>
      <pre className="overflow-auto border-t bg-white py-1 font-mono text-xs leading-relaxed">
        {file.hunks.map((h, j) => (
          <div key={j}>
            <div className="bg-muted/60 px-3 py-0.5 text-muted-foreground">{h.header}</div>
            <Highlighted
              path={file.path}
              lines={h.lines.map((l) => l.text)}
              row={(i, code) => {
                const l = h.lines[i]!;
                return (
                  <div key={i} className={cn("flex px-1", ROW[l.kind])}>
                    <span className={cn(NUM, "w-9 pr-2")}>{l.old ?? ""}</span>
                    <span className={cn(NUM, "w-9 pr-2")}>{l.new ?? ""}</span>
                    <span className="w-4 shrink-0 text-muted-foreground select-none">{SIGN[l.kind]}</span>
                    <span className="whitespace-pre">{code}</span>
                  </div>
                );
              }}
            />
          </div>
        ))}
      </pre>
    </details>
  );
}

/** A unified diff per file, folded when there are many or they are long:
 * you open the ones you care about. */
export function DiffView({ diff }: { diff: string }) {
  const files = parseDiff(diff);
  if (files.length === 0) return <p className="text-sm text-muted-foreground">(no change)</p>;
  const lines = files.reduce((n, f) => n + f.added + f.removed, 0);
  const open = files.length <= 3 && lines <= 300;
  return (
    <div className="space-y-2">
      {files.map((f) => (
        <FileDiff key={f.path} file={f} open={open} />
      ))}
    </div>
  );
}
