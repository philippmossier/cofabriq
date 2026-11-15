import { Link } from "@tanstack/react-router";
import { ExternalLink } from "lucide-react";
import { useState } from "react";
import { type FlowSummary, useIssues, useRepos } from "@/api/generated";
import { Loading, Status } from "@/components/bits";
import { StartFlow } from "@/components/start-flow";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { when } from "@/lib/format";
import { useWriteToken } from "@/lib/token";

/** Where a run starts: a repo's stored issues, each with a Start. */
export function IssuesPage() {
  const repos = useRepos({ query: { select: (r) => (r.status === 200 ? r.data : []) } });
  const [picked, setPicked] = useState<string | null>(null);
  const token = useWriteToken();
  if (repos.isPending) return <Loading what="repos" />;
  const list = repos.data ?? [];
  if (list.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No repo to run flows on: it needs its issues (<code>cofabriq fetch-issues --repo …</code>) and{" "}
        <code>[loop]</code> in its target file.
      </p>
    );
  }
  const repo = picked ?? list[0]!.repo;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-4">
        <h1 className="font-heading text-xl font-semibold">Issues</h1>
        {list.length > 1 ? (
          <select
            className="rounded-md border px-2 py-1 text-sm"
            value={repo}
            onChange={(e) => setPicked(e.target.value)}
          >
            {list.map((r) => (
              <option key={r.repo} value={r.repo}>
                {r.repo}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-sm text-muted-foreground">{repo}</span>
        )}
      </div>
      <p className="text-sm text-muted-foreground">
        Start reads the issue fresh from GitHub, refuses one with an open blocker, then an agent reads the
        code and writes a spec (about 20 s, one paid agent session). Nothing is written before you approve
        the plan.
      </p>
      {!token && (
        <p className="text-sm text-muted-foreground">
          Read-only here: open the URL that <code>cofabriq serve --write</code> printed to start flows.
        </p>
      )}
      <IssueTable key={repo} repo={repo} canStart={token !== null} />
    </div>
  );
}

function IssueTable({ repo, canStart }: { repo: string; canStart: boolean }) {
  const [owner, name] = repo.split("/") as [string, string];
  const q = useIssues(owner, name, { query: { select: (r) => (r.status === 200 ? r.data : []) } });
  const [search, setSearch] = useState("");
  const [closed, setClosed] = useState(false);
  if (q.isPending) return <Loading what="issues" />;
  const needle = search.trim().toLowerCase().replace(/^#/, "");
  const rows = (q.data ?? []).filter(
    (i) =>
      (closed || i.state === "open") &&
      (!needle || String(i.number) === needle || i.title.toLowerCase().includes(needle)),
  );
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-4 text-sm">
        <input
          className="w-72 rounded-md border px-3 py-1"
          placeholder="Search by number or title"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label className="flex items-center gap-1.5 text-muted-foreground">
          <input type="checkbox" checked={closed} onChange={(e) => setClosed(e.target.checked)} />
          closed too
        </label>
        <span className="text-muted-foreground">{rows.length} issues</span>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Issue</TableHead>
            <TableHead>Labels</TableHead>
            <TableHead>Updated</TableHead>
            <TableHead className="w-80">Run</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((i) => (
            <TableRow key={i.number}>
              <TableCell className="max-w-xl">
                <div className="flex items-center gap-2">
                  <span className="truncate">
                    <span className="text-muted-foreground">#{i.number}</span> {i.title}
                  </span>
                  <a
                    href={i.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="on GitHub"
                    className="shrink-0 text-muted-foreground hover:text-foreground"
                  >
                    <ExternalLink className="size-3.5" />
                  </a>
                  {i.state === "closed" && <Badge variant="outline">closed</Badge>}
                </div>
              </TableCell>
              <TableCell>
                <div className="flex flex-wrap gap-1">
                  {i.labels.map((l) => (
                    <Badge key={l} variant="secondary">
                      {l}
                    </Badge>
                  ))}
                </div>
              </TableCell>
              <TableCell className="text-sm text-muted-foreground">{when(i.updated_at)}</TableCell>
              <TableCell>
                {i.flow ? (
                  <Link
                    to="/flows/$owner/$name/$number"
                    params={{ owner, name, number: String(i.number) }}
                    className="flex items-center gap-2 text-sm hover:underline"
                  >
                    <FlowState flow={i.flow} />
                    <span className="font-medium">Open →</span>
                  </Link>
                ) : (
                  <StartFlow owner={owner} name={name} number={i.number} canStart={canStart} />
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

/** Where an issue's flow stands, as one badge: working, waiting at a gate,
 * or how it ended. */
export function FlowState({ flow }: { flow: FlowSummary }) {
  if (flow.running) return <Badge variant="secondary">working…</Badge>;
  if (flow.waiting) return <Badge>waits at the {flow.waiting} gate</Badge>;
  return (
    <span className="flex items-center gap-1.5">
      <Status value={flow.outcome.split(":")[0]!} />
      <span className="text-xs text-muted-foreground">{flow.route}</span>
    </span>
  );
}
