import { useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useState } from "react";
import { type FlowView, useFlowAnswer } from "@/api/generated";
import { Button } from "@/components/ui/button";
import { refreshFlow } from "@/lib/flows";
import { dropToken, useWriteToken, writeAuth } from "@/lib/token";

/** What to look for before answering: the gate's job in a few lines. */
const CHECK: Record<"plan" | "test", string[]> = {
  plan: [
    "The cause names the code the issue is about, with the lines (yellow).",
    "The change is no bigger than the cause needs.",
    "The proof would fail for the reported reason, not a nearby one.",
    "The guesses under “Check these” hold.",
  ],
  test: [
    "It fails on the unchanged code, and the failure lines show the reported problem.",
    "It checks what the issue describes, not an implementation detail.",
    "Once approved it is frozen: the build must make exactly this test pass.",
  ],
};

export function GateChecklist({ gate }: { gate: "plan" | "test" }) {
  return (
    <ul className="space-y-1 text-sm">
      {CHECK[gate].map((c) => (
        <li key={c} className="flex gap-2">
          <span className="text-muted-foreground">□</span>
          {c}
        </li>
      ))}
    </ul>
  );
}

export type Sent = { approve: boolean; again: boolean } | null;

/** The answer to a gate, in a bar fixed to the bottom of the window: the
 * page above it is long, and the decision comes after reading it. */
export function GateBar({ f, onSent }: { f: FlowView; onSent: (s: Sent) => void }) {
  const token = useWriteToken();
  const client = useQueryClient();
  const answer = useFlowAnswer({ fetch: writeAuth() });
  const [reason, setReason] = useState("");
  const gate = f.waiting!;
  const [owner, name] = f.repo.split("/") as [string, string];
  const canAnswer = !f.running && token !== null;
  // again: at the test gate, a rejection that asks the test writer for another test
  const send = (approve: boolean, again = false) => {
    onSent({ approve, again });
    const data = { approve, reason: approve ? "" : reason, again };
    answer.mutate(
      { owner, name, number: f.number, data },
      {
        onSuccess: async (r) => {
          if ((r.status as number) === 401) {
            dropToken(); // the server restarted: a dead token
            return;
          }
          setReason(""); // answered: the next gate (or a new flow) starts empty
          // it says "running" at once: don't wait for the next poll
          await refreshFlow(client, owner, name, f.number);
        },
      },
    );
  };
  // which button was pressed: it gets the spinner
  const sent = answer.variables?.data;
  const sending = !answer.isPending ? null : sent?.approve ? "approve" : sent?.again ? "again" : "reject";
  // the guard's 401 / 403 aren't in the schema: a plain number
  const refused: number | null = answer.data && answer.data.status !== 200 ? answer.data.status : null;
  const busy = !canAnswer || answer.isPending;

  return (
    <div
      role="region"
      aria-label="your answer"
      className="fixed inset-x-0 bottom-0 z-10 border-t bg-background/95 shadow-[0_-4px_12px_rgba(0,0,0,0.06)] backdrop-blur"
    >
      <div className="mx-auto max-w-6xl space-y-1.5 px-6 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-2 text-sm font-medium">
            {gate === "plan" ? "The plan:" : "The test:"}
          </span>
          <Button disabled={busy} onClick={() => send(true)}>
            {sending === "approve" && <LoaderCircle className="animate-spin" />}
            {gate === "plan" ? "Approve the plan" : "Approve and freeze the test"}
          </Button>
          {gate === "test" && f.retests_left > 0 && (
            <Button
              variant="outline"
              disabled={busy || !reason.trim()}
              title={reason.trim() ? "" : "Say what is wrong with it first: the test writer reads it"}
              onClick={() => send(false, true)}
            >
              {sending === "again" && <LoaderCircle className="animate-spin" />}
              Write it again ({f.retests_left} left)
            </Button>
          )}
          <Button variant="outline" disabled={busy} onClick={() => send(false)}>
            {sending === "reject" && <LoaderCircle className="animate-spin" />}
            {gate === "test" ? "Stop the run" : "Reject"}
          </Button>
          <input
            className="h-8 min-w-48 flex-1 rounded-md border px-3 text-sm"
            placeholder={
              gate === "test"
                ? "What is wrong with the test (the test writer reads it; kept as a lesson)"
                : "Why, when rejecting (refine reads it if you start again; kept as a lesson)"
            }
            aria-label="reason"
            maxLength={500}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={!canAnswer}
          />
        </div>
        {!token && refused !== 401 && (
          <p className="text-xs text-muted-foreground">
            Read-only here: open the URL that <code>cofabriq serve --write</code> printed to answer.
          </p>
        )}
        {refused !== null && <p className="text-xs text-destructive">{REFUSED[refused] ?? `Refused (${refused}).`}</p>}
      </div>
    </div>
  );
}

const REFUSED: Record<number, string> = {
  401: "Refused: the server restarted with a new token. Open the new link it printed.",
  403: "Refused: open the UI at the address the server printed (127.0.0.1, not localhost).",
  409: "Refused: the flow is no longer waiting here (answered in another tab or the terminal?).",
};
