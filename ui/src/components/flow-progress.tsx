import { Check, CircleDot, CirclePause, LoaderCircle, X } from "lucide-react";
import { useEffect, useState } from "react";
import { type FlowSummary, type RunStep, useTrace } from "@/api/generated";
import { Status } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { PHASES, type PhaseState, phaseStates } from "@/lib/flows";
import { duration } from "@/lib/format";
import { cn } from "@/lib/utils";

const MARK: Record<PhaseState, React.ReactNode> = {
  done: <Check className="size-3.5" />,
  now: <LoaderCircle className="size-3.5 animate-spin" />,
  waits: <CirclePause className="size-3.5" />,
  stopped: <X className="size-3.5" />,
  todo: <CircleDot className="size-3.5 opacity-40" />,
};

const TONE: Record<PhaseState, string> = {
  done: "border-emerald-600/40 bg-emerald-50 text-emerald-800",
  now: "border-primary bg-primary/5 text-foreground",
  waits: "border-primary bg-primary text-primary-foreground",
  stopped: "border-destructive/50 bg-red-50 text-destructive",
  todo: "border-border text-muted-foreground",
};

/** Where the flow is, at a glance: its seven parts, the gates marked as
 * yours; while it works, the step running now and for how long. */
export function FlowProgress({ f }: { f: FlowSummary & { trace: string } }) {
  const states = phaseStates(f);
  return (
    <div className="space-y-2">
      <ol className="flex flex-wrap items-center gap-1.5" aria-label="progress">
        {PHASES.map((p, i) => (
          <li key={p.phase} className="flex items-center gap-1.5">
            {i > 0 && <span className="h-px w-3 bg-border" />}
            <span
              className={cn(
                "flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium",
                TONE[states[i]!],
              )}
              aria-current={states[i] === "now" || states[i] === "waits" ? "step" : undefined}
              title={p.you ? "a gate: you decide" : undefined}
            >
              {MARK[states[i]!]}
              {p.label}
              {p.you && <span className="font-normal opacity-70">· you</span>}
            </span>
          </li>
        ))}
      </ol>
      {f.running && f.trace && <NowRunning run={f.trace} />}
    </div>
  );
}

/** Where an issue's flow stands, as one badge: working, waiting for you,
 * or how it ended. */
export function FlowState({ flow }: { flow: FlowSummary }) {
  if (flow.running) return <Badge variant="secondary">working…</Badge>;
  if (flow.waiting) return <Badge>waits for you: {flow.waiting === "plan" ? "the plan" : "the test"}</Badge>;
  return (
    <span className="flex items-center gap-1.5">
      <Status value={flow.outcome.split(":")[0]!} />
      <span className="text-xs text-muted-foreground">{flow.route}</span>
    </span>
  );
}

/** The deepest step that is still running: what the agent does right now. */
function current(steps: RunStep[]): RunStep[] {
  const open = steps.findLast((s) => s.status === "running");
  return open ? [open, ...current(open.children)] : [];
}

function NowRunning({ run }: { run: string }) {
  const q = useTrace(run, {
    query: { select: (r) => (r.status === 200 ? r.data : null), refetchInterval: 1000 },
  });
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const path = current(q.data?.steps ?? []);
  // the root is "Run the flow": the step under it says what runs
  const step = path[1] ?? path[0];
  const leaf = path.at(-1);
  if (!step) return <p className="text-sm text-muted-foreground">Starting…</p>;
  const since = (now - Date.parse(step.started)) / 1000;
  return (
    <p className="text-sm text-muted-foreground" aria-live="polite">
      Now: <span className="font-medium text-foreground">{step.title}</span>
      {leaf && leaf !== step && <> · {leaf.title}{leaf.detail ? `: ${leaf.detail}` : ""}</>} ·{" "}
      {duration(Math.max(since, 0))}
    </p>
  );
}
