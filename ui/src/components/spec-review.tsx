import { type ReactNode, useState } from "react";
import { type FileNote, useCode } from "@/api/generated";
import { List, Section } from "@/components/bits";
import { cn } from "@/lib/utils";

/** What the plan gate and a spec page show: the fields of a spec. */
export type SpecLike = {
  repo: string;
  commit: string;
  route: string;
  size: string | null | undefined;
  problem: string;
  files: FileNote[];
  tests: FileNote[];
  criteria: string[];
  plan: string[];
  assumptions: string[];
  open_questions: string[];
};

// `src/lib/chat/store.ts:52`, `store.ts:48`, `a.tsx:97-117`
const REF = /((?:[\w.-]+\/)*[\w.-]+\.[A-Za-z]{1,5}):(\d+)(?:-\d+)?/g;

/** A reference's file: a spec path as written, or the one spec path it is
 * the file name of; a full path the spec doesn't list is taken as is. */
function resolve(ref: string, known: string[]): string | null {
  if (known.includes(ref)) return ref;
  const by = known.filter((p) => p.endsWith(`/${ref}`));
  if (by.length === 1) return by[0]!;
  return ref.includes("/") ? ref : null;
}

function blob(s: SpecLike, path: string, line?: number): string {
  const file = path.split("/").map(encodeURIComponent).join("/");
  return `https://github.com/${s.repo}/blob/${s.commit}/${file}${line ? `#L${line}` : ""}`;
}

/** The line a spec points at in `path`: the first reference to it. */
function lineIn(s: SpecLike, path: string, texts: string[]): number | undefined {
  const known = [...s.files, ...s.tests].map((f) => f.path);
  for (const text of texts) {
    for (const m of text.matchAll(REF)) {
      if (resolve(m[1]!, known) === path) return Number(m[2]);
    }
  }
  return undefined;
}

/** The agent's text (untrusted) as React text: `code` spans, and file:line
 * references as links to GitHub at the spec's commit. Never HTML. */
function SpecText({ s, text }: { s: SpecLike; text: string }) {
  const known = [...s.files, ...s.tests].map((f) => f.path);
  const linked = (part: string, key: string): ReactNode[] => {
    const out: ReactNode[] = [];
    let last = 0;
    for (const m of part.matchAll(REF)) {
      const path = resolve(m[1]!, known);
      if (!path || !s.commit) continue;
      out.push(part.slice(last, m.index));
      out.push(
        <a
          key={`${key}-${m.index}`}
          href={blob(s, path, Number(m[2]))}
          target="_blank"
          rel="noopener noreferrer"
          className="underline decoration-dotted underline-offset-2"
        >
          {m[0]}
        </a>,
      );
      last = m.index! + m[0].length;
    }
    out.push(part.slice(last));
    return out;
  };
  return (
    <>
      {text.split("`").map((part, i) =>
        i % 2 ? (
          <code key={i} className="rounded bg-muted px-1 font-mono text-xs">
            {linked(part, String(i))}
          </code>
        ) : (
          <span key={i}>{linked(part, String(i))}</span>
        ),
      )}
    </>
  );
}

/** The file as it is now, around the line the spec points at; fetched
 * when opened. */
function CurrentCode({ s, path, line, open: initial }: { s: SpecLike; path: string; line?: number; open: boolean }) {
  const [open, setOpen] = useState(initial);
  const [owner, name] = s.repo.split("/") as [string, string];
  const q = useCode(
    owner,
    name,
    { commit: s.commit, path, line },
    { query: { enabled: open && !!s.commit, select: (r) => (r.status === 200 ? r.data : null), staleTime: Infinity } },
  );
  const c = q.data;
  return (
    <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary className="text-xs text-muted-foreground">
        current code{line ? `, around line ${line}` : ""}
      </summary>
      {open && q.isPending && <p className="text-xs text-muted-foreground">Loading…</p>}
      {open && !q.isPending && !c && <p className="text-xs text-muted-foreground">Not in the local clone.</p>}
      {c && (
        <pre className="mt-1 max-h-80 overflow-auto rounded-md bg-muted py-2 font-mono text-xs leading-relaxed">
          {c.lines.map((text, i) => {
            const n = c.start + i;
            return (
              <div key={n} className={cn("flex px-3", n === c.focus && "bg-amber-200/60")}>
                <span className="w-10 shrink-0 pr-3 text-right text-muted-foreground select-none">{n}</span>
                <span className="whitespace-pre">{text}</span>
              </div>
            );
          })}
        </pre>
      )}
    </details>
  );
}

function FileLink({ s, path, line }: { s: SpecLike; path: string; line?: number }) {
  if (!s.commit) return <code className="text-sm">{path}</code>;
  return (
    <a
      href={blob(s, path, line)}
      target="_blank"
      rel="noopener noreferrer"
      className="font-mono text-sm font-medium hover:underline"
    >
      {path} ↗
    </a>
  );
}

/** A spec in the order a reviewer reads it: the cause, the change with the
 * code as it is now, the proof, what to check, then the steps. */
export function SpecReview({ s }: { s: SpecLike }) {
  const n = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
  const texts = [s.problem, ...s.plan];
  const toCheck = s.assumptions.length + s.open_questions.length;
  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        {s.route} · size {s.size ?? "?"} · {n(s.files.length, "file changes", "files change")} · tests in{" "}
        {n(s.tests.length, "file", "files")} · {n(toCheck, "thing", "things")} to check
      </p>

      <Section title="The cause">
        <p className="text-sm leading-relaxed">
          <SpecText s={s} text={s.problem} />
        </p>
      </Section>

      <Section title="The change">
        <div className="space-y-4">
          {s.files.map((f) => {
            const line = lineIn(s, f.path, [f.why, ...texts]);
            return (
              <div key={f.path} className="space-y-1.5 rounded-md border p-3">
                <FileLink s={s} path={f.path} line={line} />
                <p className="text-sm">
                  <SpecText s={s} text={f.why} />
                </p>
                <CurrentCode s={s} path={f.path} line={line} open={s.files.length === 1} />
              </div>
            );
          })}
          {s.files.length === 0 && <p className="text-sm text-muted-foreground">(no source files)</p>}
        </div>
      </Section>

      <Section
        title={
          s.route === "feature"
            ? "The proof: acceptance criteria, each one test, frozen before the build"
            : "The proof: a test written before any code, which must fail first"
        }
      >
        {s.route === "feature" && <List items={s.criteria} ordered />}
        <ul className="space-y-1.5 text-sm">
          {s.tests.map((t) => (
            <li key={t.path}>
              <FileLink s={s} path={t.path} />{" "}
              <span className="text-muted-foreground">
                <SpecText s={s} text={t.why} />
              </span>
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Check these: where the agent guessed, what it couldn't answer">
        {toCheck === 0 && <p className="text-sm text-muted-foreground">(nothing)</p>}
        <ul className="space-y-1.5 text-sm">
          {s.assumptions.map((a, i) => (
            <li key={`a${i}`} className="flex gap-2">
              <span className="font-semibold text-amber-600">!</span>
              <span>
                <SpecText s={s} text={a} />
              </span>
            </li>
          ))}
          {s.open_questions.map((q, i) => (
            <li key={`q${i}`} className="flex gap-2">
              <span className="font-semibold text-destructive">?</span>
              <span>
                <SpecText s={s} text={q} />
              </span>
            </li>
          ))}
        </ul>
      </Section>

      <details>
        <summary className="text-sm font-medium text-muted-foreground">Plan steps ({s.plan.length})</summary>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm">
          {s.plan.map((step, i) => (
            <li key={i}>
              <SpecText s={s} text={step} />
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}
