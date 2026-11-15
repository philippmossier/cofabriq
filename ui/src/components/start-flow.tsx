import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { Route, useFlowStart } from "@/api/generated";
import { Button } from "@/components/ui/button";
import { dropToken, writeAuth } from "@/lib/token";

/** Bug or feature (the route), then Start: on success the flow's page,
 * where refine runs live; a refusal says why. `again`: replace the issue's
 * ended flow, and refine reads why it ended. */
export function StartFlow({
  owner,
  name,
  number,
  canStart,
  again = false,
  route: initial = Route.bug,
}: {
  owner: string;
  name: string;
  number: number;
  canStart: boolean;
  again?: boolean;
  route?: Route;
}) {
  const [route, setRoute] = useState<Route>(initial);
  const navigate = useNavigate();
  const start = useFlowStart({ fetch: writeAuth() });
  const send = () =>
    start.mutate(
      { owner, name, number, data: { route, again } },
      {
        onSuccess: (r) => {
          if (r.status === 200) {
            void navigate({ to: "/flows/$owner/$name/$number", params: { owner, name, number: String(number) } });
          } else if ((r.status as number) === 401) {
            dropToken(); // the server restarted with a new token
          }
        },
      },
    );
  const r = start.data;
  // the guard's 401 / 403 aren't in the schema: a plain number
  const status: number | null = r && r.status !== 200 ? r.status : null;
  const why =
    r?.status === 409
      ? r.data.detail
      : status === 401
        ? "The server restarted with a new token: open the new link it printed."
        : status === 403
          ? "Open the UI at the address the server printed (127.0.0.1, not localhost)."
          : status !== null
            ? `Refused (${status}).`
            : "";
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <select
          className="rounded-md border px-2 py-1 text-sm"
          value={route}
          onChange={(e) => setRoute(e.target.value as Route)}
          disabled={!canStart}
          title="feature: the spec gets acceptance criteria, a test gate before the build"
        >
          <option value={Route.bug}>bug</option>
          <option value={Route.feature}>feature</option>
        </select>
        <Button size="sm" disabled={!canStart || start.isPending} onClick={send}>
          {start.isPending ? "Checking…" : again ? "Start again" : "Start"}
        </Button>
      </div>
      {why && <p className="text-xs text-destructive">{why}</p>}
    </div>
  );
}
