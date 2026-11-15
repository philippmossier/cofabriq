import { ChevronRight, CircleCheck, CirclePause, CircleX, LoaderCircle } from "lucide-react";
import { useState } from "react";
import {
  type Code,
  type RunStep,
  type StepDoc,
  type traceResponse,
  usePayload,
  useStepDocs,
  useTrace,
} from "@/api/generated";
import { Loading, Pre } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { duration, usd } from "@/lib/format";
import { cn } from "@/lib/utils";

// How deep the view goes; each level shows the ones before it too.
const LEVELS = ["steps", "data", "code", "why"] as const;
type Level = (typeof LEVELS)[number];
const shows = (level: Level, min: Level) => LEVELS.indexOf(level) >= LEVELS.indexOf(min);

const POLL_MS = 1000;
// span attributes (tracing.py)
const TURNS = "cofabriq.agent.turns";
const COST = "cofabriq.cost_usd";
const SANDBOX_OK = "cofabriq.sandbox.ok";

function running(steps: RunStep[]): boolean {
  return steps.some((s) => s.status === "running" || running(s.children));
}

function endsAt(s: RunStep): number {
  const start = Date.parse(s.started);
  return s.ms === null ? Date.now() : start + s.ms;
}

/** A run's steps as a tree: each step's time on its part of the run, what
 * it cost, and on demand what it got and gave, its code and why it exists.
 * Polls while a step runs; `live` polls even before the first one starts
 * (an answer was just sent). */
export function RunSteps({ run, live = false }: { run: string; live?: boolean }) {
  const [level, setLevel] = useState<Level>("steps");
  const q = useTrace(run, {
    query: {
      select: (r) => (r.status === 200 ? r.data : null),
      refetchInterval: (query) => {
        const r: traceResponse | undefined = query.state.data;
        return live || (r?.status === 200 && running(r.data.steps)) ? POLL_MS : false;
      },
    },
  });
  const docs = useStepDocs({
    query: { select: (r) => (r.status === 200 ? r.data : {}), staleTime: Infinity },
  });
  if (q.isPending) return <Loading what="the steps" />;
  const tree = q.data;
  if (!tree) return <p className="text-sm text-muted-foreground">No steps recorded for this run.</p>;
  const t = tree.totals;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Tabs value={level} onValueChange={(v) => setLevel(v as Level)}>
          <TabsList>
            <TabsTrigger value="steps">Steps</TabsTrigger>
            <TabsTrigger value="data">+ Data</TabsTrigger>
            <TabsTrigger value="code">+ Code</TabsTrigger>
            <TabsTrigger value="why">+ Why</TabsTrigger>
          </TabsList>
        </Tabs>
        <span className="text-sm text-muted-foreground">
          {duration(t.seconds)} (gates included) · {t.agents} agent sessions · {t.turns} turns ·{" "}
          {usd(t.cost_usd)} list price
        </span>
      </div>
      {level !== "steps" && (
        <p className="text-xs text-muted-foreground">Click a step to open its details.</p>
      )}
      <div className="rounded-md border p-2">
        {tree.steps.map((root, i) => {
          const prev = tree.steps[i - 1];
          const waited = prev ? (Date.parse(root.started) - endsAt(prev)) / 1000 : 0;
          return (
            <div key={root.id}>
              {prev && (
                <p className="my-1 border-y border-dashed py-1 pl-6 text-xs text-muted-foreground">
                  waited {duration(Math.max(waited, 0))} for your answer at the gate
                </p>
              )}
              <Row run={run} step={root} root={root} depth={0} level={level} docs={docs.data ?? {}} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

type RowProps = {
  run: string;
  step: RunStep;
  root: RunStep; // the bar's scale: this part of the run, not the gate waits
  depth: number;
  level: Level;
  docs: Record<string, StepDoc>;
};

function Row({ run, step, root, depth, level, docs }: RowProps) {
  const [open, setOpen] = useState(true);
  const [details, setDetails] = useState(false);
  const doc = docs[step.doc];
  const hasDetails = (shows(level, "data") && step.payload) || (shows(level, "why") && !!doc);
  const indent = { paddingLeft: depth * 20 };
  const turns = step.attrs[TURNS];
  const cost = step.attrs[COST];

  return (
    <div>
      <div className="flex items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-muted/50" style={indent}>
        {step.children.length > 0 ? (
          <button
            type="button"
            onClick={() => setOpen(!open)}
            aria-label={open ? "collapse" : "expand"}
            className="text-muted-foreground"
          >
            <ChevronRight className={cn("size-4 transition-transform", open && "rotate-90")} />
          </button>
        ) : (
          <span className="w-4 shrink-0" />
        )}
        <Mark status={step.status} />
        <button
          type="button"
          disabled={!hasDetails}
          onClick={() => setDetails(!details)}
          title={doc?.summary}
          className={cn("min-w-0 truncate text-left", hasDetails && "cursor-pointer hover:underline")}
        >
          <span className={depth <= 1 ? "font-medium" : ""}>{step.title}</span>
          {step.detail && <span className="text-muted-foreground">: {step.detail}</span>}
        </button>
        {step.attrs[SANDBOX_OK] === false && (
          <Badge variant="outline" title="the command failed: wanted when the test must be red">
            red
          </Badge>
        )}
        {shows(level, "code") && step.code && <CodeLink code={step.code} />}
        <span className="ml-auto flex shrink-0 items-center gap-3 text-xs text-muted-foreground tabular-nums">
          {turns !== undefined && <span>{String(turns)} turns</span>}
          {typeof cost === "number" && <span>{usd(cost)}</span>}
          <Bar step={step} root={root} />
          <span className="w-16 text-right">
            {step.ms === null ? "running" : duration(step.ms / 1000)}
          </span>
        </span>
      </div>
      {shows(level, "data") && (
        <Facts step={step} open={details} onToggle={() => setDetails(!details)} indent={depth * 20 + 52} />
      )}
      {shows(level, "why") && depth === 1 && doc && (
        <p className="pb-1 text-xs text-muted-foreground" style={{ paddingLeft: depth * 20 + 52 }}>
          <Inline text={doc.summary} />
        </p>
      )}
      {step.error && (
        <p className="text-xs text-destructive" style={{ paddingLeft: depth * 20 + 52 }}>
          {step.error}
        </p>
      )}
      {details && hasDetails && (
        <div style={{ paddingLeft: depth * 20 + 52 }} className="space-y-3 py-2 pr-2">
          {shows(level, "why") && doc && <Why doc={doc} />}
          {shows(level, "data") && step.payload && <Payload run={run} span={step.id} />}
        </div>
      )}
      {open &&
        step.children.map((c) => (
          <Row key={c.id} run={run} step={c} root={root} depth={depth + 1} level={level} docs={docs} />
        ))}
    </div>
  );
}

// already on the row (detail, chips) or meaningless to a reader
const ON_ROW = new Set([
  TURNS,
  COST,
  "gen_ai.operation.name",
  "gen_ai.provider.name",
  "gen_ai.agent.name",
  "gen_ai.request.model",
  "gen_ai.response.model",
  "cofabriq.llm.what",
  "cofabriq.sandbox.command",
  "cofabriq.sandbox.network",
  SANDBOX_OK,
  "cofabriq.paused",
  "cofabriq.resume",
]);

function fact(key: string, value: RunStep["attrs"][string]): string {
  const name = key.replace(/^(gen_ai|cofabriq)\.(agent\.|sandbox\.|usage\.)?/, "");
  if (Array.isArray(value)) return `${name}: ${value.join(", ")}`;
  if (typeof value === "number" && key.endsWith("_usd")) return `${name.replace(/_usd$/, "")}: ${usd(value)}`;
  return `${name}: ${String(value)}`;
}

/** The Data level, without a click: the span's other attributes, and the
 * toggle for what the step got and gave. */
function Facts({ step, open, onToggle, indent }: { step: RunStep; open: boolean; onToggle: () => void; indent: number }) {
  const facts = Object.entries(step.attrs)
    .filter(([k]) => !ON_ROW.has(k) && !k.startsWith("code."))
    .map(([k, v]) => fact(k, v));
  if (facts.length === 0 && !step.payload) return null;
  return (
    <div className="flex flex-wrap gap-x-3 pb-1 text-xs text-muted-foreground" style={{ paddingLeft: indent }}>
      {facts.map((f) => (
        <span key={f}>{f}</span>
      ))}
      {step.payload && (
        <button type="button" onClick={onToggle} className="font-medium text-foreground hover:underline">
          {open ? "▾" : "▸"} what it got and gave
        </button>
      )}
    </div>
  );
}

function Mark({ status }: { status: RunStep["status"] }) {
  const cls = "size-4 shrink-0";
  if (status === "running") return <LoaderCircle className={cn(cls, "animate-spin text-primary")} />;
  if (status === "paused") return <CirclePause className={cn(cls, "text-amber-600")} />;
  if (status === "error") return <CircleX className={cn(cls, "text-destructive")} />;
  return <CircleCheck className={cn(cls, "text-emerald-600")} />;
}

/** Where the step ran within its part of the run, as a bar. */
function Bar({ step, root }: { step: RunStep; root: RunStep }) {
  const t0 = Date.parse(root.started);
  const total = Math.max(endsAt(root) - t0, 1);
  const left = ((Date.parse(step.started) - t0) / total) * 100;
  const width = ((endsAt(step) - Date.parse(step.started)) / total) * 100;
  return (
    <span className="relative h-1.5 w-32 rounded bg-muted">
      <span
        className={cn(
          "absolute h-full rounded",
          step.status === "error" ? "bg-destructive" : step.status === "running" ? "animate-pulse bg-primary" : "bg-foreground/40",
        )}
        style={{ left: `${Math.min(left, 100)}%`, width: `${Math.max(width, 0.5)}%` }}
      />
    </span>
  );
}

/** `build_stage · run.py:212`, linked at the commit that ran. A graph node is
 * a closure (`build_graph.<locals>.record`), an agent a callable class
 * (`ClaudeCode.__call__`): the name shown is the one a reader looks for. */
function CodeLink({ code }: { code: Code }) {
  const name = code.function.split(".").filter((p) => p !== "<locals>" && p !== "__call__").at(-1);
  const label = `${name} · ${code.file.split("/").at(-1)}:${code.line}`;
  const title = `${code.function}\n${code.file}:${code.line}`;
  const cls = "shrink-0 font-mono text-xs text-muted-foreground";
  if (!code.url) return <code className={cls} title={title}>{label}</code>;
  return (
    <a href={code.url} target="_blank" rel="noopener noreferrer" title={title} className={cn(cls, "hover:underline")}>
      {label}
    </a>
  );
}

/** Our own doc text (docs/steps/), with its `code` spans; all the Markdown
 * the docs use. */
function Inline({ text }: { text: string }) {
  return text
    .replace(/\s+/g, " ")
    .split("`")
    .map((part, i) => (i % 2 ? <code key={i} className="font-mono text-xs">{part}</code> : part));
}

function Why({ doc }: { doc: StepDoc }) {
  return (
    <div className="space-y-2 rounded-md border-l-2 pl-3 text-sm">
      <p className="font-medium">
        <Inline text={doc.summary} />
      </p>
      {doc.why.split(/\n\s*\n/).map((para, i) => (
        <p key={i} className="text-muted-foreground">
          <Inline text={para} />
        </p>
      ))}
    </div>
  );
}

/** What the step got and gave. Prompts, answers and output are untrusted:
 * text only, never HTML or Markdown. */
function Payload({ run, span }: { run: string; span: string }) {
  const q = usePayload(run, span, {
    query: { select: (r) => (r.status === 200 ? r.data : null), staleTime: Infinity },
  });
  if (q.isPending) return <Loading what="the data" />;
  if (!q.data) return <p className="text-xs text-muted-foreground">Nothing kept for this step.</p>;
  return (
    <div className="space-y-2">
      {Object.entries(q.data).map(([key, value]) => (
        <div key={key} className="space-y-1">
          <div className="text-xs font-medium text-muted-foreground">{key}</div>
          <Pre text={typeof value === "string" ? value : JSON.stringify(value, null, 2)} />
        </div>
      ))}
    </div>
  );
}
