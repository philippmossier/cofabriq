import { useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { getIssuesQueryKey, getReposQueryKey, useGithubToken, useImportIssues } from "@/api/generated";
import { Button } from "@/components/ui/button";
import { dropToken, writeAuth } from "@/lib/token";

const LIMIT = 50;

/** The newest open issues from GitHub into cofabriq's store: read-only towards
 * GitHub. More open than the cap: it asks before importing the newest ones.
 * Says which token reads, and warns when that token could write. */
export function ImportIssues({ owner, name, canWrite }: { owner: string; name: string; canWrite: boolean }) {
  const client = useQueryClient();
  const access = useGithubToken({ query: { select: (r) => (r.status === 200 ? r.data : null) } }).data;
  const run = useImportIssues({ fetch: writeAuth() });
  const send = (confirm: boolean) =>
    run.mutate(
      { owner, name, data: { limit: LIMIT, confirm } },
      {
        onSuccess: async (r) => {
          if ((r.status as number) === 401) {
            dropToken(); // the server restarted with a new token
            return;
          }
          if (r.status === 200 && !r.data.asked) {
            await Promise.all([
              client.invalidateQueries({ queryKey: getIssuesQueryKey(owner, name) }),
              client.invalidateQueries({ queryKey: getReposQueryKey() }),
            ]);
          }
        },
      },
    );
  const r = run.data;
  const done = r?.status === 200 ? r.data : null;
  // the guard's 401 / 403 and the 502 / 503 aren't in the schema: a plain number
  const status: number | null = r && r.status !== 200 ? r.status : null;
  const noToken = access !== undefined && access?.source == null;
  return (
    <div className="space-y-1 text-sm">
      <div className="flex flex-wrap items-center gap-3">
        <Button size="sm" variant="outline" onClick={() => send(false)} disabled={!canWrite || noToken || run.isPending}>
          {run.isPending && <LoaderCircle className="size-4 animate-spin" />}
          Import open issues
        </Button>
        <span className="text-muted-foreground">
          {noToken
            ? "No GitHub token: set GITHUB_TOKEN (read-only: Issues and Metadata) to import."
            : `The newest ${LIMIT}, read with ${access?.source === "gh" ? "gh's login" : "GITHUB_TOKEN"}; cofabriq never writes to GitHub.`}
        </span>
      </div>
      {access?.warning && <p className="text-destructive">Warning: {access.warning}.</p>}
      {done?.asked && (
        <div className="flex items-center gap-2" role="status">
          <span>
            {done.total} open issues on GitHub: import the newest {LIMIT}?
          </span>
          <Button size="sm" onClick={() => send(true)} disabled={run.isPending}>
            Import {LIMIT}
          </Button>
        </div>
      )}
      {done && !done.asked && (
        <p className="text-muted-foreground" role="status">
          Imported: {done.written} new or changed, {done.unchanged} unchanged, of {done.total} open.
        </p>
      )}
      {status !== null && (
        <p className="text-destructive">
          {r?.status === 409
            ? r.data.detail
            : status === 401
              ? "The server restarted with a new token: open the new link it printed."
              : status === 503
                ? "The server has no GitHub token."
                : `Refused (${status}).`}
        </p>
      )}
    </div>
  );
}
