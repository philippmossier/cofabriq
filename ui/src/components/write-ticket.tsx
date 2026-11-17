import { useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useState } from "react";
import { getIssuesQueryKey, useTicketNew } from "@/api/generated";
import { Button } from "@/components/ui/button";
import { dropToken, writeAuth } from "@/lib/token";

/** A local ticket (L1, L2, ...): written here, kept in cofabriq's data/, never
 * sent to GitHub. A check reads it first and lists what is missing; it fills
 * nothing in. With gaps it is saved only on "Save anyway". */
export function WriteTicket({ owner, name, canWrite }: { owner: string; name: string; canWrite: boolean }) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [saved, setSaved] = useState("");
  const client = useQueryClient();
  const write = useTicketNew({ fetch: writeAuth() });
  const send = (anyway: boolean) =>
    write.mutate(
      { owner, name, data: { title, body, anyway } },
      {
        onSuccess: async (r) => {
          if ((r.status as number) === 401) {
            dropToken(); // the server restarted with a new token
            return;
          }
          if (r.status === 200 && r.data.saved) {
            setSaved(r.data.saved.ticket);
            setTitle("");
            setBody("");
            setOpen(false); // the box closes, the line says where it went
            write.reset();
            await client.invalidateQueries({ queryKey: getIssuesQueryKey(owner, name) });
          }
        },
      },
    );
  const r = write.data;
  const gaps = r?.status === 200 && !r.data.saved ? r.data : null;
  // the guard's 401 / 403 / 503 aren't in the schema: a plain number
  const status: number | null = r && r.status !== 200 ? r.status : null;
  if (!open) {
    return (
      <div className="flex items-center gap-3 text-sm">
        <Button size="sm" variant="outline" onClick={() => setOpen(true)} disabled={!canWrite}>
          Write a ticket
        </Button>
        {saved && <span className="text-muted-foreground">Saved as {saved}: it is in the list below.</span>}
      </div>
    );
  }
  return (
    <div className="space-y-2 rounded-md border p-3 text-sm" role="form" aria-label="write a ticket">
      <p className="text-muted-foreground">
        A local ticket stays on this machine (cofabriq's <code>data/tickets/</code>) and is never sent to GitHub.
        One check (about $0.001) says what is missing before it is saved; it fills nothing in.
      </p>
      <input
        className="w-full rounded-md border px-3 py-1"
        placeholder="Title: what is wrong, or what is wanted"
        value={title}
        maxLength={200}
        onChange={(e) => setTitle(e.target.value)}
      />
      <textarea
        className="h-32 w-full rounded-md border px-3 py-1 font-mono text-xs"
        placeholder={"Steps, what happens, what should happen (a bug)\nor what it should do, for whom (a feature)"}
        value={body}
        onChange={(e) => setBody(e.target.value)}
      />
      {gaps && (
        <div className="space-y-1" role="status">
          <p className="font-medium">Reads as a {gaps.type}. Not saved, it lacks:</p>
          <ul className="list-disc pl-5 text-muted-foreground">
            {gaps.missing.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </div>
      )}
      {status !== null && (
        <p className="text-destructive">
          {status === 401
            ? "The server restarted with a new token: open the new link it printed."
            : status === 503
              ? "The server has no OPENAI_API_KEY: the check can't run."
              : `Refused (${status}).`}
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={() => send(false)} disabled={!canWrite || !title.trim() || write.isPending}>
          {write.isPending && <LoaderCircle className="size-4 animate-spin" />}
          Check and save
        </Button>
        {gaps && (
          <Button size="sm" variant="outline" onClick={() => send(true)} disabled={write.isPending}>
            Save anyway
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
          Close
        </Button>
      </div>
    </div>
  );
}
