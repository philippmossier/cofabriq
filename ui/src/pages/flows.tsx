import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { useFlow, useFlowAnswer, useFlowList } from "@/api/generated";
import { FlowResult } from "@/components/result-card";
import { StartFlow } from "@/components/start-flow";
import { againRoute } from "@/lib/flows";
import { List, Loading, Missing, Pre, Section, Status } from "@/components/bits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { RunSteps } from "@/components/run-steps";
import { dropToken, useWriteToken, writeAuth } from "@/lib/token";

const POLL_MS = 3000;

export function FlowsPage() {
  const q = useFlowList({
    query: {
      select: (r) => (r.status === 200 ? r.data : []),
      refetchInterval: POLL_MS,
    },
  });
  if (q.isPending) return <Loading what="flows" />;
  const rows = q.data ?? [];
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h1 className="font-heading text-xl font-semibold">Flows</h1>
        <span className="text-sm text-muted-foreground">
          runs waiting for you: start one from <Link to="/issues" className="underline">Issues</Link>
          {" "}(or <code>cofabriq flow start &lt;n&gt;</code>)
        </span>
      </div>
      {rows.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No flows (or the server runs without <code>--write</code>).
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
                    {f.running ? (
                      <Badge variant="secondary">working…</Badge>
                    ) : f.waiting ? (
                      <Badge>waits at the {f.waiting} gate</Badge>
                    ) : (
                      <Status value={f.outcome.split(":")[0]!} />
                    )}
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

export function FlowPage() {
  const { owner, name, number } = useParams({ from: "/flows/$owner/$name/$number" });
  const q = useFlow(owner, name, Number(number), {
    query: {
      select: (r) => (r.status === 200 ? r.data : null),
      refetchInterval: POLL_MS,
    },
  });
  const token = useWriteToken();
  const answer = useFlowAnswer({ fetch: writeAuth() });
  const [reason, setReason] = useState("");
  if (q.isPending) return <Loading what="the flow" />;
  const f = q.data;
  if (!f) return <Missing what="flow" />;
  const canAnswer = f.waiting !== null && !f.running && token !== null;
  const send = (approve: boolean) =>
    answer.mutate(
      { owner, name, number: Number(number), data: { approve, reason: approve ? "" : reason } },
      // 401: the server restarted with a new token; this one is dead
      { onSuccess: (r) => (r.status as number) === 401 && dropToken() },
    );
  // the guard's 401 / 403 aren't in the schema: a plain number
  const refused: number | null =
    answer.data && answer.data.status !== 200 ? answer.data.status : null;

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <Link to="/flows" className="text-sm text-muted-foreground hover:underline">
          ← Flows
        </Link>
        <h1 className="font-heading text-xl font-semibold">
          #{f.number} {f.title}
        </h1>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Badge variant="secondary">{f.route}</Badge>
          {f.size && <Badge variant="outline">size {f.size}</Badge>}
          <span>{f.repo}</span>
        </div>
      </div>

      <Card className={f.waiting ? "border-primary" : ""}>
        <CardHeader>
          <CardTitle>
            {f.running && !f.problem
              ? "Reading the code to write a spec… (about 20 s; the plan gate comes next)"
              : f.running
              ? "Working on your answer… (setup, test, build and review take a few minutes)"
              : f.waiting === "plan"
                ? "Approve the plan? No code is written before you do."
                : f.waiting === "test"
                  ? "Approve the test? It is frozen from here: the build can't change it."
                  : `Done: ${f.outcome}`}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {f.error && <p className="text-sm text-destructive">{f.error}</p>}
          {f.waiting && !token && refused !== 401 && (
            <p className="text-sm text-muted-foreground">
              Read-only here: open the URL that <code>cofabriq serve --write</code> printed.
            </p>
          )}
          {!f.waiting && !f.running && f.record && <FlowResult file={f.record} number={f.number} />}
          {!f.waiting && !f.running && (
            <div className="space-y-1 border-t pt-3">
              <p className="text-sm text-muted-foreground">
                {f.outcome === "approved"
                  ? "Run it again: a new flow replaces this one (nothing was merged, so the bug is still there). The old run stays under Runs."
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
          {f.waiting && (
            <div className="flex gap-2">
              <Button disabled={!canAnswer || answer.isPending} onClick={() => send(true)}>
                Approve
              </Button>
              <Button
                variant="outline"
                disabled={!canAnswer || answer.isPending}
                onClick={() => send(false)}
              >
                Reject
              </Button>
              <input
                className="min-w-0 flex-1 rounded-md border px-3 text-sm"
                placeholder="Why, when rejecting (becomes a lesson)"
                maxLength={500}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                disabled={!canAnswer}
              />
            </div>
          )}
          {answer.isPending && <p className="text-sm text-muted-foreground">Sending…</p>}
          {refused === 401 && (
            <p className="text-sm text-destructive">
              Refused: the server restarted with a new token. Open the new link it printed.
            </p>
          )}
          {refused === 403 && (
            <p className="text-sm text-destructive">
              Refused: open the UI at the address the server printed (127.0.0.1, not localhost).
            </p>
          )}
          {refused !== null && refused !== 401 && refused !== 403 && (
            <p className="text-sm text-destructive">Refused ({refused}).</p>
          )}
        </CardContent>
      </Card>

      {f.trace && (
        <Section title="Steps: what ran, what it cost, why">
          <RunSteps run={f.trace} live={f.running} />
        </Section>
      )}

      {f.waiting === "test" && (
        <Section title="The test (frozen once approved)">
          <Pre text={f.diff} />
        </Section>
      )}

      <Section title="Problem">
        <Pre text={f.problem} className="bg-transparent p-0 font-sans text-sm" />
      </Section>
      {f.route === "feature" && (
        <Section title="Acceptance criteria">
          <List items={f.criteria} ordered />
        </Section>
      )}
      <Section title="Files">
        <List items={f.files.map((x) => `${x.path}: ${x.why}`)} />
      </Section>
      <Section title="Plan">
        <List items={f.plan} ordered />
      </Section>
      <Section title="Open questions (answered by approving the plan)">
        <List items={f.open_questions} />
      </Section>
    </div>
  );
}
