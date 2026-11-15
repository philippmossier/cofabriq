import { Link } from "@tanstack/react-router";
import type { RunTrace } from "@/api/generated";
import { Loading } from "@/components/bits";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useRun } from "@/api/generated";
import { parseDiff } from "@/lib/diff";
import { usd } from "@/lib/format";

/** The outcome in one sentence: the review gate's status, else the loop's. */
const OUTCOME: Record<string, string> = {
  approved: "Fixed: the test failed before the change and passes after it, and the reviewer approved.",
  unproven: "Fixed, with reviewer concerns no test could prove: they are listed in the draft PR.",
  blocked: "Blocked: the reviewer proved a problem with a failing test. No PR.",
  green: "Fixed: the test failed before the change and passes after it. No review ran.",
  escalated: "Not fixed in 3 attempts: a person takes it from here.",
  not_red: "The test passed on the current code: the bug didn't reproduce, so nothing was changed.",
  no_repro: "No failing test could be written for it, so nothing was changed.",
  setup_failed: "The target's install failed in the sandbox.",
  test_rejected: "You rejected the test, so nothing was built.",
  halted: "Stopped by the kill switch or the run budget.",
};

/** What a finished run did, at a glance: outcome, change, proof, review,
 * cost, and where the PR is. The sections below it have the detail. */
export function ResultCard({ t }: { t: RunTrace }) {
  const outcome = t.gate ?? t.status;
  const files = parseDiff(t.diff);
  const review = t.rounds.at(-1);
  const bad = ["blocked", "escalated", "setup_failed", "halted"].includes(outcome);
  // an approval with open concerns is not a clean "fixed": they lead the card
  const open = (review?.findings ?? []).filter((f) => f.severity !== "nit");
  const title =
    outcome === "approved" && open.length > 0
      ? `Approved, with ${open.length} open concern${open.length === 1 ? "" : "s"}: read ${open.length === 1 ? "it" : "them"} before you trust the fix.`
      : (OUTCOME[outcome] ?? outcome);
  return (
    <Card className={bad ? "border-destructive/50" : open.length > 0 ? "border-amber-400" : "border-primary/40"}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {open.length > 0 && (
          <Row label="Concerns">
            <ul className="list-disc space-y-1 pl-4">
              {open.map((f, i) => (
                <li key={i}>
                  <code className="text-xs">
                    {f.path}
                    {f.line ? `:${f.line}` : ""}
                  </code>{" "}
                  {f.text}
                </li>
              ))}
            </ul>
          </Row>
        )}
        {files.length > 0 && (
          <Row label="Changed">
            {files.map((f) => (
              <div key={f.path}>
                <code className="text-xs">{f.path}</code>{" "}
                <span className="text-emerald-700">+{f.added}</span>{" "}
                <span className="text-destructive">−{f.removed}</span>
              </div>
            ))}
          </Row>
        )}
        {t.red && (
          <Row label="Proof">
            the agent's test failed on the unchanged code ({t.red.name}), then passed with the change
          </Row>
        )}
        {review && <Row label="Review">{review.summary}</Row>}
        <Row label="Cost">
          {usd(t.cost_usd)} list price, setup to review (refine is counted in the steps below)
        </Row>
        <Row label="PR">
          {t.pr_url ? (
            <a href={t.pr_url} target="_blank" rel="noopener noreferrer" className="underline">
              {t.pr_url}
            </a>
          ) : bad || !t.pr_body ? (
            "none"
          ) : (
            "not opened (dry run): the draft PR body is at the bottom of this page"
          )}
        </Row>
      </CardContent>
    </Card>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[5rem_1fr] gap-2">
      <span className="text-muted-foreground">{label}</span>
      <div>{children}</div>
    </div>
  );
}

/** A flow's run record as a result card, with the link to all of it. */
export function FlowResult({ file, number }: { file: string; number: number }) {
  const q = useRun(file, String(number), {
    query: { select: (r) => (r.status === 200 ? r.data : null) },
  });
  if (q.isPending) return <Loading what="the result" />;
  if (!q.data) return null;
  return (
    <div className="space-y-2">
      <ResultCard t={q.data} />
      <Link
        to="/runs/$file/$key"
        params={{ file, key: String(number) }}
        className="text-sm font-medium hover:underline"
      >
        The whole run: test, diff, review, draft PR →
      </Link>
    </div>
  );
}
