import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { useFlow, useFlowAnswer, useFlowList } from "@/api/generated";
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
import { writeAuth, writeToken } from "@/lib/token";

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
          runs waiting for you: start one with <code>cofabriq flow start &lt;n&gt;</code>
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
  const answer = useFlowAnswer({ fetch: writeAuth() });
  const [reason, setReason] = useState("");
  if (q.isPending) return <Loading what="the flow" />;
  const f = q.data;
  if (!f) return <Missing what="flow" />;
  const canAnswer = f.waiting !== null && !f.running && writeToken() !== null;
  const send = (approve: boolean) =>
    answer.mutate({
      owner,
      name,
      number: Number(number),
      data: { approve, reason: approve ? "" : reason },
    });

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
            {f.running
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
          {f.waiting && !writeToken() && (
            <p className="text-sm text-muted-foreground">
              Read-only here: open the URL that <code>cofabriq serve --write</code> printed.
            </p>
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
          {answer.data && answer.data.status !== 200 && (
            <p className="text-sm text-destructive">
              Refused ({answer.data.status}): the token may be stale; open the printed URL again.
            </p>
          )}
        </CardContent>
      </Card>

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
