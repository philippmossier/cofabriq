import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { type RunKind, type RunSummary, type Step, useRun, useRuns } from "@/api/generated";
import { Checks, List, Loading, Missing, Pre, Section, Status, Verdict } from "@/components/bits";
import { usd, when } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

type Filter = "all" | RunKind;

export function RunsPage() {
  const [kind, setKind] = useState<Filter>("all");
  const q = useRuns({ query: { select: (r) => (r.status === 200 ? r.data : []) } });
  if (q.isPending) return <Loading what="runs" />;
  const rows = (q.data ?? []).filter((r: RunSummary) => kind === "all" || r.kind === kind);
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h1 className="font-heading text-xl font-semibold">Runs</h1>
        <Tabs value={kind} onValueChange={(v) => setKind(v as Filter)}>
          <TabsList>
            <TabsTrigger value="all">All</TabsTrigger>
            <TabsTrigger value="run">run</TabsTrigger>
            <TabsTrigger value="eval">eval</TabsTrigger>
            <TabsTrigger value="features">features</TabsTrigger>
            <TabsTrigger value="gate">gate</TabsTrigger>
            <TabsTrigger value="wrong">wrong</TabsTrigger>
          </TabsList>
        </Tabs>
        <span className="text-sm text-muted-foreground">{rows.length} runs</span>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Key</TableHead>
            <TableHead>Kind</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Judge</TableHead>
            <TableHead>Agent's test</TableHead>
            <TableHead className="text-right">Tries</TableHead>
            <TableHead className="text-right">Cost</TableHead>
            <TableHead>Made</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={`${r.file}/${r.key}`}>
              <TableCell>
                <Link
                  to="/runs/$file/$key"
                  params={{ file: r.file, key: r.key }}
                  className="font-medium hover:underline"
                >
                  {r.key}
                </Link>
              </TableCell>
              <TableCell>
                <Badge variant="secondary">{r.kind}</Badge>
              </TableCell>
              <TableCell>
                <Status value={r.status} />
              </TableCell>
              <TableCell>
                <Verdict ok={r.judge} yes="green" no="red" />
              </TableCell>
              <TableCell>
                <Verdict ok={r.valid} yes="valid" no="invalid" />
              </TableCell>
              <TableCell className="text-right">{r.tries}</TableCell>
              <TableCell className="text-right">{usd(r.cost_usd)}</TableCell>
              <TableCell className="text-sm text-muted-foreground">{when(r.made_at)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

const PHASE: Record<Step["phase"], string> = {
  test: "Writes the failing test",
  build: "Implements",
  fix: "Fixes the proven findings",
};

/** One agent attempt: what it said, what got reverted, which checks went red. */
function Attempt({ step }: { step: Step }) {
  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex items-center gap-2 text-sm">
        <span className="font-medium">
          {PHASE[step.phase]}, attempt {step.n}
        </span>
        <span className="text-muted-foreground">
          {Math.round(step.seconds)} s, {usd(step.cost_usd)}
        </span>
        <span className="ml-auto">
          <Checks checks={step.checks} />
        </span>
      </div>
      {step.summary && <p className="text-sm">{step.summary}</p>}
      {step.error && <p className="text-sm text-destructive">{step.error}</p>}
      {step.reverted.length > 0 && (
        <p className="text-xs text-muted-foreground">Reverted (outside its write boundary): {step.reverted.join(", ")}</p>
      )}
      {step.checks
        .filter((c) => !c.ok && c.output)
        .map((c, i) => (
          <details key={i} className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">{c.name} output</summary>
            <Pre text={c.output} />
          </details>
        ))}
    </div>
  );
}

export function RunPage() {
  const { file, key } = useParams({ from: "/runs/$file/$key" });
  const q = useRun(file, key, { query: { select: (r) => (r.status === 200 ? r.data : null) } });
  if (q.isPending) return <Loading what="the run" />;
  const t = q.data;
  if (!t) return <Missing what="run" />;
  const tests = t.steps.filter((s) => s.phase === "test");
  const builds = t.steps.filter((s) => s.phase === "build");
  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <Link to="/runs" className="text-sm text-muted-foreground hover:underline">
          ← Runs
        </Link>
        <h1 className="font-heading text-xl font-semibold">{t.key}</h1>
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <Badge variant="secondary">{t.kind}</Badge>
          <Status value={t.status} />
          <span>judge</span>
          <Verdict ok={t.judge} yes="green" no="red" />
          <span>agent's test on the reference</span>
          <Verdict ok={t.valid} yes="valid" no="invalid" />
          <span>
            · {t.repo} at {t.commit.slice(0, 7)} · {usd(t.cost_usd)}
          </span>
        </div>
      </div>

      <Section title="1. The test, before any code">
        {tests.length === 0 && <p className="text-sm text-muted-foreground">The repro was handed in (planted).</p>}
        {tests.map((s, i) => (
          <Attempt key={i} step={s} />
        ))}
        {t.red && (
          <p className="text-sm">
            Red on the unchanged code: <Checks checks={[t.red]} />
          </p>
        )}
      </Section>

      <Section title="2. The change">
        {builds.map((s, i) => (
          <Attempt key={i} step={s} />
        ))}
      </Section>

      {t.rounds.length > 0 && (
        <Section title={`3. Review gate: ${t.gate}`}>
          {t.rounds.map((r, n) => (
            <Card key={n}>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  Review {n + 1}
                  <Status value={r.approved ? "approved" : r.verdict} />
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <p className="text-sm">{r.summary}</p>
                <List
                  items={r.findings.map(
                    (f) => `[${f.severity}] ${f.path}${f.line ? `:${f.line}` : ""}: ${f.text}`,
                  )}
                />
                {r.proofs.map((p, i) => (
                  <div key={i} className="space-y-1 rounded-md border p-3 text-sm">
                    <div className="flex items-center gap-2">
                      <Badge variant={p.proven ? "destructive" : "outline"}>
                        {p.proven ? "proven by a red test" : "unproven"}
                      </Badge>
                      <span className="text-muted-foreground">{p.finding.severity}</span>
                    </div>
                    <p>{p.finding.text}</p>
                    {p.quote && (
                      <p className="text-muted-foreground">
                        Spec: “{p.quote}” · {p.tests.join(", ")}
                      </p>
                    )}
                  </div>
                ))}
                {r.fix.map((s, i) => (
                  <Attempt key={i} step={s} />
                ))}
              </CardContent>
            </Card>
          ))}
        </Section>
      )}

      <Section title={`Diff (${t.changed.length} files)`}>
        {t.diff ? <Pre text={t.diff} /> : <p className="text-sm text-muted-foreground">(no change)</p>}
      </Section>

      {t.pr_body && (
        <Section title="Draft PR body">
          <Pre text={t.pr_body} />
        </Section>
      )}
    </div>
  );
}
