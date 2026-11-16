import { Link, useParams } from "@tanstack/react-router";
import { ExternalLink } from "lucide-react";
import { useState } from "react";
import { type FlowView, Phase, useFlow, useFlowList } from "@/api/generated";
import { FlowResult } from "@/components/result-card";
import { StartFlow } from "@/components/start-flow";
import { failureLines } from "@/lib/failure";
import { againRoute, endedWell, issueUrl } from "@/lib/flows";
import { Fold, Loading, Missing, Pre, Section } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { DiffView } from "@/components/code";
import { FlowProgress, FlowState } from "@/components/flow-progress";
import { GateBar, type Sent } from "@/components/gate";
import { GitHubLink, IssueText } from "@/components/issue-text";
import { specFacts } from "@/lib/spec";
import { RunSteps, StepsFold } from "@/components/run-steps";
import { SpecReview } from "@/components/spec-review";
import { useWriteToken } from "@/lib/token";

const POLL_MS = 3000;

export function FlowsPage() {
  const q = useFlowList({
    query: {
      select: (r) => (r.status === 200 ? r.data : []),
      refetchInterval: POLL_MS,
    },
  });
  if (q.isPending) return <Loading what="flows" />;
  // yours first: a flow at a gate waits for nobody else
  const rows = [...(q.data ?? [])].sort((a, b) => Number(!!b.waiting) - Number(!!a.waiting));
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h1 className="font-heading text-xl font-semibold">Flows</h1>
        <span className="text-sm text-muted-foreground">
          one per issue, the ones waiting for you first; start one from{" "}
          <Link to="/issues" className="underline">Issues</Link> (or <code>cofabriq flow start &lt;n&gt;</code>)
        </span>
      </div>
      {rows.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No flows yet (or the server runs without <code>--write</code>): start one from{" "}
          <Link to="/issues" className="underline">Issues</Link>.
        </p>
      )}
      {rows.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Issue</TableHead>
              <TableHead>Route</TableHead>
              <TableHead>State</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((f) => {
              const [owner, name] = f.repo.split("/");
              return (
                <TableRow key={f.thread}>
                  <TableCell>
                    <Link
                      to="/flows/$owner/$name/$number"
                      params={{ owner: owner!, name: name!, number: String(f.number) }}
                      className="font-medium hover:underline"
                    >
                      #{f.number} {f.title}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">{f.route}</Badge>
                  </TableCell>
                  <TableCell>
                    <FlowState flow={f} />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

/** The headline: what the flow does now, the question at a gate, or how it
 * ended. `sent` is this page's last answer, which the server doesn't echo. */
function headline(f: FlowView, sent: Sent): string {
  if (f.running) {
    if (!f.problem) return "Reading the code to write a spec… (about 20 s; the plan comes to you next)";
    if (sent && !sent.approve && !sent.again) return "Rejected: closing the flow…";
    if (sent && !sent.approve && sent.again)
      return "Writing another test with your reason… (the test comes back to you)";
    if (f.phase === Phase.test)
      return "Setting up and writing a failing test… (a minute or two; you check the test next)";
    return "Building the fix against your test, then a review… (a few minutes; nothing waits for you)";
  }
  if (f.waiting === "plan") return "Approve the plan? No code before you do.";
  if (f.waiting === "test") return "Approve the test? From here it is frozen.";
  if (f.outcome.startsWith("cut off")) return `Stopped mid-run (${f.outcome}): Start again below.`;
  // "rejected: plan rejected: why", "failed: not ready: why": the note says it
  if (/^(rejected|failed): /.test(f.outcome)) {
    const note = f.outcome.replace(/^(rejected|failed): /, "");
    return f.outcome.startsWith("failed") ? `Done: sent back before any code, ${note}` : `Done: ${note}`;
  }
  return `Done: ${f.outcome}`;
}

export function FlowPage() {
  const { owner, name, number } = useParams({ from: "/flows/$owner/$name/$number" });
  const q = useFlow(owner, name, Number(number), {
    query: {
      select: (r) => (r.status === 200 ? r.data : null),
      // while it works, each second: a rejection ends it in under one
      refetchInterval: (query) => {
        const r = query.state.data;
        return r?.status === 200 && r.data.running ? 1000 : POLL_MS;
      },
    },
  });
  const token = useWriteToken();
  const [sent, setSent] = useState<Sent>(null);
  if (q.isPending) return <Loading what="the flow" />;
  const f = q.data;
  if (!f) return <Missing what="flow" />;
  const ended = !f.waiting && !f.running;
  // the header carries the spec's summary line
  const spec = f.problem ? <SpecReview s={f} facts={false} /> : null;
  const github = <GitHubLink url={issueUrl(f.repo, f.number)} />;
  const issue = <IssueText body={f.body} />;
  // at a gate the steps so far are context, not what is judged: folded
  const steps =
    f.trace &&
    (f.waiting ? (
      <StepsFold run={f.trace} />
    ) : (
      <Section title="Steps: what ran, what it cost, why">
        <RunSteps run={f.trace} live={f.running} />
      </Section>
    ));

  return (
    // room for the fixed answer bar, so it never covers the page's end
    <div className={f.waiting ? "space-y-6 pb-32" : "space-y-6"}>
      <div className="space-y-2">
        <Link to="/flows" className="text-sm text-muted-foreground hover:underline">
          ← Flows
        </Link>
        <h1 className="flex items-center gap-2 font-heading text-xl font-semibold">
          #{f.number} {f.title}
          <a
            href={issueUrl(f.repo, f.number)}
            target="_blank"
            rel="noopener noreferrer"
            title="the issue on GitHub"
            className="text-muted-foreground hover:text-foreground"
          >
            <ExternalLink className="size-4" />
          </a>
          <span className="text-sm font-normal text-muted-foreground">{f.repo}</span>
        </h1>
        <p className="flex flex-wrap items-center gap-x-1.5 text-sm text-muted-foreground">
          <Badge variant="secondary">{f.route}</Badge>
          <span>{(f.problem ? specFacts(f) : []).join(" · ")}</span>
        </p>
        <FlowProgress f={f} />
        {f.error && <p className="text-sm text-destructive">{f.error}</p>}
      </div>

      {/* at a gate the question is in the answer bar */}
      {!f.waiting && (
        <Card>
          <CardContent className="space-y-3">
            <h2 data-testid="flow-state" className="font-heading text-base font-medium">
              {headline(f, sent)}
            </h2>
            {ended && f.record && <FlowResult file={f.record} number={f.number} />}
            {ended && (
              <div className="space-y-1 border-t pt-3">
                <p className="text-sm text-muted-foreground">
                  {endedWell(f.outcome)
                    ? "Run it again: a new flow replaces this one (nothing was merged, so the issue is still open). The old run stays under Runs."
                    : "Wrong type, or a plan you rejected? Start again: a new flow replaces this one, and refine reads why this one ended. The old run stays under Runs."}
                </p>
                <StartFlow
                  owner={owner}
                  name={name}
                  number={f.number}
                  canStart={token !== null}
                  route={againRoute(f)}
                  again
                />
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {f.waiting === "plan" && (
        <>
          <Section title="The issue, as reported" aside={github}>
            {issue}
          </Section>
          {spec}
        </>
      )}

      {f.waiting === "test" && (
        <>
          <Section title="How it fails on the unchanged code">
            <p className="text-sm text-muted-foreground">
              Is this the reported problem? A test can fail for a reason the issue never asked about; here a
              wrong reason is still cheap to catch.
            </p>
            <Pre text={failureLines(f.red).join("\n") || "(no failing line found: see the full output)"} />
            <details>
              <summary className="text-xs text-muted-foreground">the full output</summary>
              <Pre text={f.red} />
            </details>
          </Section>
          <Section title="The test (frozen once approved)">
            <DiffView diff={f.diff} />
          </Section>
          {spec && <Fold title="The plan you approved: cause, change, proof">{spec}</Fold>}
          <Fold title="The issue, as reported">
            {issue}
            <div className="mt-1">{github}</div>
          </Fold>
        </>
      )}

      {steps}
      {!f.waiting && spec && <Fold title="The spec: cause, change, proof">{spec}</Fold>}
      {!f.waiting && (
        <Fold title="The issue, as reported">
          {issue}
          <div className="mt-1">{github}</div>
        </Fold>
      )}

      {f.waiting && (
        <GateBar key={f.waiting} f={f} onSent={setSent}>
          <span data-testid="flow-state">{headline(f, sent)}</span>
        </GateBar>
      )}
    </div>
  );
}
