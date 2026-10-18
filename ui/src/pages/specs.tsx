import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { type SpecSummary, useSpec, useSpecs } from "@/api/generated";
import { List, Loading, Missing, Pre, Section } from "@/components/bits";
import { usd, when } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

type Route = "all" | "feature" | "bug";

/** The readiness verdict: ready, the reasons it goes back, or not checked. */
function Readiness({ reasons }: { reasons: string[] | null | undefined }) {
  if (reasons === null || reasons === undefined) {
    return <span className="text-muted-foreground" title="no clone to check paths against">-</span>;
  }
  if (reasons.length === 0) return <Badge variant="outline">ready</Badge>;
  return (
    <Badge variant="destructive" title={reasons.join("; ")}>
      back to dev
    </Badge>
  );
}

export function SpecsPage() {
  const [route, setRoute] = useState<Route>("all");
  const q = useSpecs({ query: { select: (r) => (r.status === 200 ? r.data : []) } });
  if (q.isPending) return <Loading what="specs" />;
  const rows = (q.data ?? []).filter((s: SpecSummary) => route === "all" || s.route === route);
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h1 className="font-heading text-xl font-semibold">Specs</h1>
        <Tabs value={route} onValueChange={(v) => setRoute(v as Route)}>
          <TabsList>
            <TabsTrigger value="all">All</TabsTrigger>
            <TabsTrigger value="feature">Features</TabsTrigger>
            <TabsTrigger value="bug">Bugs</TabsTrigger>
          </TabsList>
        </Tabs>
        <span className="text-sm text-muted-foreground">{rows.length} specs</span>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Key</TableHead>
            <TableHead>Route</TableHead>
            <TableHead>Size</TableHead>
            <TableHead className="text-right">Files</TableHead>
            <TableHead className="text-right">Criteria</TableHead>
            <TableHead className="text-right">Questions</TableHead>
            <TableHead>Readiness</TableHead>
            <TableHead>Made</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((s) => (
            <TableRow key={`${s.file}/${s.key}`}>
              <TableCell>
                <Link
                  to="/specs/$file/$key"
                  params={{ file: s.file, key: s.key }}
                  className="font-medium hover:underline"
                >
                  {s.key}
                </Link>
                <div className="text-xs text-muted-foreground">
                  {s.repo}
                  {s.number > 0 ? `#${s.number}` : " (local ticket)"}
                </div>
              </TableCell>
              <TableCell>
                <Badge variant="secondary">{s.route}</Badge>
              </TableCell>
              <TableCell>{s.size}</TableCell>
              <TableCell className="text-right">{s.files}</TableCell>
              <TableCell className="text-right">{s.route === "feature" ? s.criteria : "-"}</TableCell>
              <TableCell className="text-right">{s.open_questions || "-"}</TableCell>
              <TableCell>
                <Readiness reasons={s.not_ready} />
              </TableCell>
              <TableCell className="text-sm text-muted-foreground">{when(s.made_at)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function SpecPage() {
  const { file, key } = useParams({ from: "/specs/$file/$key" });
  const q = useSpec(file, key, { query: { select: (r) => (r.status === 200 ? r.data : null) } });
  if (q.isPending) return <Loading what="the spec" />;
  const s = q.data;
  if (!s) return <Missing what="spec" />;
  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <Link to="/specs" className="text-sm text-muted-foreground hover:underline">
          ← Specs
        </Link>
        <h1 className="font-heading text-xl font-semibold">{s.key}</h1>
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <Badge variant="secondary">{s.route}</Badge>
          <Badge variant="outline">size {s.size}</Badge>
          <span>
            {s.repo} at {s.commit.slice(0, 7)}
          </span>
          <span>
            · {s.turns} turns, {Math.round(s.seconds)} s, {usd(s.cost_usd)}
          </span>
        </div>
      </div>

      {s.not_ready && s.not_ready.length > 0 && (
        <Card className="border-destructive/50">
          <CardHeader>
            <CardTitle className="text-destructive">Back to the dev</CardTitle>
          </CardHeader>
          <CardContent>
            <List items={s.not_ready} />
          </CardContent>
        </Card>
      )}

      <Section title="Problem">
        <Pre text={s.problem} className="bg-transparent p-0 font-sans text-sm" />
      </Section>

      {s.route === "feature" && (
        <Card>
          <CardHeader>
            <CardTitle>Acceptance criteria</CardTitle>
          </CardHeader>
          <CardContent>
            <List items={s.criteria} ordered />
          </CardContent>
        </Card>
      )}

      <div className="grid gap-6 md:grid-cols-2">
        <Section title="Files">
          <List items={s.files.map((f) => `${f.path}: ${f.why}`)} />
        </Section>
        <Section title="Tests">
          <List items={s.tests.map((f) => `${f.path}: ${f.why}`)} />
        </Section>
      </div>

      <Section title="Plan">
        <List items={s.plan} ordered />
      </Section>

      <Separator />

      <div className="grid gap-6 md:grid-cols-2">
        <Section title="Assumptions">
          <List items={s.assumptions} />
        </Section>
        <Section title="Open questions (answered by approving the plan)">
          <List items={s.open_questions} />
        </Section>
      </div>

      {s.unknown_paths.length > 0 && (
        <Section title="Paths not in the repo at this commit">
          <List items={s.unknown_paths} />
        </Section>
      )}
    </div>
  );
}
