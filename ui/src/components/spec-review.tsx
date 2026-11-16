import { type ReactNode, useState } from "react";
import { type FileNote, type ProofNote, useCode } from "@/api/generated";
import { cn } from "@/lib/utils";
import { PROOF_SHORT, specFacts } from "@/lib/spec";
import { List, Section } from "@/components/bits";
import { CodeLines, ProposedEdit } from "@/components/code";
import { Badge } from "@/components/ui/badge";

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
  unknown_paths: string[]; // not in the repo at the commit: files to create
  proof?: ProofNote | null; // how a failing test will show it; none on older specs
};

/** How the fix is proven, as the spec says: a proxy is a stand-in to judge. */
const PROOF: Record<ProofNote["kind"], { label: string; tone: string }> = {
  unit: { label: "A unit test:", tone: "border-emerald-500" },
  browser: { label: "A browser test:", tone: "border-emerald-500" },
  proxy: {
    label: "A stand-in measure (judge whether it measures the reported problem):",
    tone: "border-amber-500 bg-amber-50",
  },
  none: { label: "No test can prove it here:", tone: "border-destructive bg-red-50" },
};

// `src/lib/chat/store.ts:52`, `store.ts:48`, `a.tsx:97-117`
const REF = /((?:[\w.-]+\/)*[\w.-]+\.[A-Za-z]{1,5}):(\d+)(?:-(\d+))?/g;
const AROUND = 8; // the server's lines above and below a range (serve.py)

/** What a marked line is: one the change touches, or one the spec only
 * mentions (as evidence, in the cause). */
type Mark = "change" | "mention";

/** One snippet: lines `from` to `to`, with the referenced lines marked. */
type Span = { from: number; to: number; marks: Map<number, Mark> };

const MARK: Record<Mark, string> = { change: "bg-amber-200/70", mention: "bg-sky-100" };

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

type Ref = [from: number, to: number, mark: Mark];

function range(from: number, to: number | undefined): [number, number] {
  const end = to === undefined ? from : Math.max(from, to);
  return [from, Math.min(end, from + 200)]; // a typo'd range stays a snippet
}

/** The places of `file` the spec names: the lines its change touches
 * (`at`, checked by refine; older specs: what the file's own sentence
 * cites), then the ones the rest of the spec only mentions. Merged into
 * one snippet where their windows would touch. */
function spansIn(s: SpecLike, file: FileNote, texts: string[]): Span[] {
  const known = [...s.files, ...s.tests].map((f) => f.path);
  const cited = (text: string): [number, number][] =>
    [...text.matchAll(REF)]
      .filter((m) => resolve(m[1]!, known) === file.path)
      .map((m) => range(Number(m[2]), m[3] ? Number(m[3]) : undefined));
  // in the file's own sentence a bare "line 74" / "lines 100-107" is this file
  const bare = [...file.why.matchAll(/\blines? (\d+)(?:\s*-\s*(\d+))?/gi)].map((m) =>
    range(Number(m[1]), m[2] ? Number(m[2]) : undefined),
  );
  const change: [number, number][] =
    file.at.length > 0
      ? file.at.map((a) => {
          const [from, to] = a.split("-").map(Number);
          return range(from!, to);
        })
      : [...cited(file.why), ...bare];
  const refs: Ref[] = [
    ...change.map(([f, t]): Ref => [f, t, "change"]),
    ...texts.flatMap(cited).map(([f, t]): Ref => [f, t, "mention"]),
  ].sort((a, b) => a[0] - b[0]);
  const spans: Span[] = [];
  for (const [from, to, mark] of refs) {
    const last = spans.at(-1);
    if (last && from - last.to <= 2 * AROUND + 1) last.to = Math.max(last.to, to);
    else spans.push({ from, to, marks: new Map() });
    const span = spans.at(-1)!;
    for (let n = from; n <= to; n++) {
      if (span.marks.get(n) !== "change") span.marks.set(n, mark); // a change wins
    }
  }
  return spans;
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
      // a file the change creates has no page on GitHub yet
      if (!path || !s.commit || s.unknown_paths.includes(path)) continue;
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
  // only `a span` (single backticks, no line break inside) is code; a fence
  // (```) or a stray backtick stays as written
  const parts = text.split(/(?<!`)`([^`\n]+)`(?!`)/);
  return (
    <>
      {parts.map((part, i) =>
        i % 2 ? (
          <code key={i} className="rounded bg-muted px-1 font-mono text-xs">
            {linked(part, String(i))}
          </code>
        ) : (
          <span key={i} className="whitespace-pre-line">
            {linked(part, String(i))}
          </span>
        ),
      )}
    </>
  );
}

/** The file as it is now around a span the spec points at (the top of
 * the file without one); fetched when opened. */
function CurrentCode({ s, path, span, open: initial }: { s: SpecLike; path: string; span?: Span; open: boolean }) {
  const [open, setOpen] = useState(initial);
  const [owner, name] = s.repo.split("/") as [string, string];
  const q = useCode(
    owner,
    name,
    { commit: s.commit, path, line: span?.from, end: span?.to },
    { query: { enabled: open && !!s.commit, select: (r) => (r.status === 200 ? r.data : null), staleTime: Infinity } },
  );
  const c = q.data;
  const where = !span ? "" : span.from === span.to ? `, line ${span.from}` : `, lines ${span.from}-${span.to}`;
  return (
    <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary className="text-xs text-muted-foreground">current code{where}</summary>
      {open && q.isPending && <p className="text-xs text-muted-foreground">Loading…</p>}
      {open && !q.isPending && !c && <p className="text-xs text-muted-foreground">Not in the local clone.</p>}
      {c && (
        <CodeLines
          path={path}
          start={c.start}
          lines={c.lines}
          mark={(n) => {
            const m = span?.marks.get(n);
            return m ? MARK[m] : undefined;
          }}
        />
      )}
    </details>
  );
}

function FileLink({ s, path, line }: { s: SpecLike; path: string; line?: number }) {
  if (s.unknown_paths.includes(path)) {
    return (
      <span className="inline-flex items-center gap-2">
        <code className="text-sm font-medium">{path}</code>
        <Badge variant="outline">new file</Badge>
      </span>
    );
  }
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

/** The agent's prose in sentences: a sentence ends at . ! or ? before
 * white space and a capital, a backtick or a parenthesis ("e.g. a",
 * "store.ts:5" don't end one). Line breaks inside one stay, so a fence
 * keeps its lines. */
function sentences(text: string): string[] {
  return text
    .split(/(?<=[.!?])\s+(?=[A-Z`(])/)
    .map((t) => t.trim())
    .filter(Boolean);
}

/** The cause as a lead (what is wrong, where: the first sentences, about
 * two lines) and the evidence behind it, folded in short paragraphs. */
function Cause({ s }: { s: SpecLike }) {
  const all = sentences(s.problem);
  let lead = 1;
  while (lead < all.length && lead < 3 && all.slice(0, lead).join(" ").length < 160) lead++;
  const rest = all.slice(lead);
  const paras: string[] = [];
  for (let i = 0; i < rest.length; i += 3) paras.push(rest.slice(i, i + 3).join(" "));
  return (
    <div className="space-y-2">
      <p className="text-[0.95rem] leading-relaxed">
        <SpecText s={s} text={all.slice(0, lead).join(" ")} />
      </p>
      {paras.length > 0 && (
        <details className="group">
          <summary className="cursor-pointer text-xs text-muted-foreground select-none">
            how the agent found it ({rest.length} {rest.length === 1 ? "sentence" : "sentences"})
          </summary>
          <div className="mt-2 space-y-2 border-l-2 pl-3 text-sm leading-relaxed text-muted-foreground">
            {paras.map((p, i) => (
              <p key={i}>
                <SpecText s={s} text={p} />
              </p>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

/** The planned test as the runner will print it: the cases that must fail
 * now (the bug) red, the ones that must stay as they are green. */
function PlannedTest({ s }: { s: SpecLike }) {
  const cases = s.proof?.cases ?? [];
  return (
    <div className="rounded-md border bg-muted/30 p-3 font-mono text-xs">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {s.tests.map((t) => (
          <FileLink key={t.path} s={s} path={t.path} />
        ))}
        {s.proof && <span className="font-sans text-muted-foreground">{PROOF_SHORT[s.proof.kind]}</span>}
      </div>
      <ul className="space-y-1.5">
        {cases.map((c, i) => (
          <li key={i} className="grid grid-cols-[1rem_1fr] gap-x-2">
            <span className={c.fails_now ? "text-destructive" : "text-emerald-700"}>{c.fails_now ? "✗" : "✓"}</span>
            <span>
              <span className="font-medium text-foreground">{c.name}</span>
              <span className="ml-2 rounded bg-background px-1 font-sans text-[0.7rem] text-muted-foreground">
                {c.fails_now ? "fails now" : "passes now, must stay"}
              </span>
            </span>
            <span />
            <span className="font-sans text-muted-foreground">
              <SpecText s={s} text={c.expect} />
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** A spec in the order a reviewer reads it: the cause, the change with the
 * code as it is now, the proof, what to check, then the steps. `gate`: it
 * is being judged, so each part says what to check in it. `facts`: its
 * summary line (`specFacts`); off where the page header carries it. */
export function SpecReview({ s, gate = false, facts = true }: { s: SpecLike; gate?: boolean; facts?: boolean }) {
  const texts = [s.problem, ...s.plan, ...s.assumptions];
  const toCheck = s.assumptions.length + s.open_questions.length;
  const cases = s.proof?.cases ?? [];
  // the code around a sketch is folded: the colours only matter without one
  const marked = s.files.some((f) => f.edits.length === 0 && !s.unknown_paths.includes(f.path));
  return (
    <div className="space-y-6">
      {facts && <p className="text-sm text-muted-foreground">{[s.route, ...specFacts(s)].join(" · ")}</p>}

      <Section title="The cause" check={gate ? "does it point at the code the issue is about?" : undefined}>
        <Cause s={s} />
      </Section>

      <Section title="The change" check={gate ? "is it no bigger than the cause needs?" : undefined}>
        {marked && (
          <p className="text-xs text-muted-foreground">
            <span className="rounded bg-amber-200/70 px-1">yellow</span>: the lines the change touches ·{" "}
            <span className="rounded bg-sky-100 px-1">blue</span>: lines the spec only mentions
          </p>
        )}
        <div className="space-y-4">
          {s.files.map((f) => {
            const spans = spansIn(s, f, texts);
            const isNew = s.unknown_paths.includes(f.path);
            return (
              <div key={f.path} className="space-y-1.5 rounded-md border p-3">
                <FileLink s={s} path={f.path} line={spans[0]?.from} />
                <p className="text-sm">
                  <SpecText s={s} text={f.why} />
                </p>
                {f.edits.length > 0 && (
                  <div className="space-y-1.5">
                    <p className="text-xs text-muted-foreground">
                      proposed by the agent, not run: its old lines are checked against the file; the build
                      writes the real change against the test
                    </p>
                    {f.edits.map((e, i) => (
                      <ProposedEdit key={i} path={f.path} line={e.line} old={e.old} next={e.new} />
                    ))}
                  </div>
                )}
                {isNew ? (
                  <p className="text-xs text-muted-foreground">Not in the repo yet: the change creates it.</p>
                ) : spans.length === 0 ? (
                  <CurrentCode s={s} path={f.path} open={false} />
                ) : (
                  spans.map((span) => (
                    <CurrentCode
                      key={span.from}
                      s={s}
                      path={f.path}
                      span={span}
                      // with sketches, the code around them is one click away
                      open={s.files.length === 1 && spans.length <= 3 && f.edits.length === 0}
                    />
                  ))
                )}
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
        check={
          !gate
            ? undefined
            : s.route === "feature"
              ? "does each criterion say what the request asks, checkable by one test?"
              : "would the red case fail for the reported reason, not a nearby one?"
        }
      >
        {cases.length > 0 ? (
          <>
            <PlannedTest s={s} />
            {s.proof!.kind === "proxy" && (
              <div className={cn("rounded-md border-l-4 py-1 pl-3 text-sm", PROOF.proxy.tone)}>
                <span className="font-medium">{PROOF.proxy.label}</span> <SpecText s={s} text={s.proof!.how} />
              </div>
            )}
          </>
        ) : (
          s.proof && (
            <div className={cn("rounded-md border-l-4 py-1 pl-3 text-sm", PROOF[s.proof.kind].tone)}>
              <span className="font-medium">{PROOF[s.proof.kind].label}</span>{" "}
              <SpecText s={s} text={s.proof.how} />
            </div>
          )
        )}
        {s.route === "feature" && <List items={s.criteria} ordered />}
        {cases.length > 0 ? (
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground select-none">
              the test writer's notes: how it fails now, where it goes
            </summary>
            <div className="mt-2 space-y-1.5 text-sm text-muted-foreground">
              {s.proof && (
                <p>
                  <SpecText s={s} text={s.proof.how} />
                </p>
              )}
              {s.tests.map((t) => (
                <p key={t.path}>
                  <code className="text-xs">{t.path}</code>: <SpecText s={s} text={t.why} />
                </p>
              ))}
            </div>
          </details>
        ) : (
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
        )}
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
