import { Link } from "@tanstack/react-router";
import { ChevronRight, ExternalLink } from "lucide-react";
import { Fragment, useState } from "react";
import { type IssueRow, useFlowList, useIssue, useIssues, useRepos } from "@/api/generated";
import { Loading } from "@/components/bits";
import { FlowState } from "@/components/flow-progress";
import { IssueText } from "@/components/issue-text";
import { StartFlow } from "@/components/start-flow";
import { againRoute } from "@/lib/flows";
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
import { cn } from "@/lib/utils";
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
      <WaitingForYou />
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
        Open an issue to read it, pick bug or feature, then Start: it reads the issue fresh from GitHub, refuses
        one with an open blocker, then an agent reads the code and writes a spec (about 20 s, one paid agent
        session). You approve the plan, then the failing test, before any code is written.
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

/** Flows paused at a gate, on top of the page you start from: nothing
 * goes on until you answer. */
function WaitingForYou() {
  const q = useFlowList({
    query: { select: (r) => (r.status === 200 ? r.data.filter((f) => f.waiting && !f.running) : []) },
  });
  const waiting = q.data ?? [];
  if (waiting.length === 0) return null;
  return (
    <div className="rounded-md border border-primary bg-primary/5 p-3 text-sm" role="status">
      <p className="mb-1 font-medium">Waiting for you</p>
      <ul className="space-y-0.5">
        {waiting.map((f) => {
          const [owner, name] = f.repo.split("/") as [string, string];
          return (
            <li key={f.thread}>
              <Link
                to="/flows/$owner/$name/$number"
                params={{ owner, name, number: String(f.number) }}
                className="hover:underline"
              >
                #{f.number} {f.title}: approve {f.waiting === "plan" ? "the plan" : "the test"} →
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function IssueTable({ repo, canStart }: { repo: string; canStart: boolean }) {
  const [owner, name] = repo.split("/") as [string, string];
  const q = useIssues(owner, name, { query: { select: (r) => (r.status === 200 ? r.data : []) } });
  const [search, setSearch] = useState("");
  const [closed, setClosed] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
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
            <Fragment key={i.number}>
              <TableRow>
                <TableCell className="max-w-xl">
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setOpen(open === i.number ? null : i.number)}
                      aria-expanded={open === i.number}
                      className="flex min-w-0 items-center gap-1 text-left hover:underline"
                    >
                      <ChevronRight
                        className={cn(
                          "size-4 shrink-0 text-muted-foreground transition-transform",
                          open === i.number && "rotate-90",
                        )}
                      />
                      <span className="truncate">
                        <span className="text-muted-foreground">#{i.number}</span> {i.title}
                      </span>
                    </button>
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
                  <RunCell i={i} owner={owner} name={name} canStart={canStart} />
                </TableCell>
              </TableRow>
              {open === i.number && (
                <TableRow className="hover:bg-transparent">
                  <TableCell colSpan={4} className="whitespace-normal">
                    <Preview owner={owner} name={name} i={i} />
                  </TableCell>
                </TableRow>
              )}
            </Fragment>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function RunCell({ i, owner, name, canStart }: { i: IssueRow; owner: string; name: string; canStart: boolean }) {
  if (!i.flow) return <StartFlow owner={owner} name={name} number={i.number} canStart={canStart} />;
  return (
    <div className="space-y-1.5">
      <Link
        to="/flows/$owner/$name/$number"
        params={{ owner, name, number: String(i.number) }}
        className="flex items-center gap-2 text-sm hover:underline"
      >
        <FlowState flow={i.flow} />
        <span className="font-medium">Open →</span>
      </Link>
      {!i.flow.running && !i.flow.waiting && (
        <StartFlow
          owner={owner}
          name={name}
          number={i.number}
          canStart={canStart}
          route={againRoute(i.flow)}
          again
        />
      )}
    </div>
  );
}

/** The issue's text as last fetched: enough to pick bug or feature, and to
 * see whether it says what is wrong before paying for a spec. */
function Preview({ owner, name, i }: { owner: string; name: string; i: IssueRow }) {
  const q = useIssue(owner, name, i.number, { query: { select: (r) => (r.status === 200 ? r.data : null) } });
  if (q.isPending) return <Loading what="the issue" />;
  if (!q.data) return <p className="text-sm text-destructive">Not in the store any more.</p>;
  return (
    <div className="space-y-1 py-1">
      <IssueText body={q.data.body} url={i.url} />
      <p className="text-xs text-muted-foreground">
        As last fetched ({i.comments} comment{i.comments === 1 ? "" : "s"} on GitHub); Start reads it fresh.
      </p>
    </div>
  );
}
