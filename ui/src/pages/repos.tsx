import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { CircleCheck, CircleX, LoaderCircle } from "lucide-react";
import { useState } from "react";
import {
  type RepoSetup,
  type SetupCitation,
  type SetupPhase,
  type SetupStep,
  getRepoSetupQueryKey,
  getRepoSetupsQueryKey,
  getReposQueryKey,
  useRepoAdd,
  useRepoCheck,
  useRepoSetup,
  useRepoSetups,
} from "@/api/generated";
import { Loading, Missing, Pre, Section } from "@/components/bits";
import { ThreePlaces } from "@/components/three-places";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { usd } from "@/lib/format";
import { dropToken, useWriteToken, writeAuth } from "@/lib/token";

const POLL_MS = 1000;

const PHASE: Record<SetupPhase, string> = {
  proposing: "proposing",
  draft: "read, edit, check",
  checking: "checking",
  proven: "proven",
  failed: "check failed",
};

function PhaseBadge({ phase }: { phase: SetupPhase }) {
  const variant = phase === "proven" ? "default" : phase === "failed" ? "destructive" : "secondary";
  return <Badge variant={variant}>{PHASE[phase]}</Badge>;
}

/** The dev's own repos and adding one: a path in, a proposed config out,
 * checked in the sandbox before any flow may run on it. */
export function ReposPage() {
  const q = useRepoSetups({ query: { select: (r) => (r.status === 200 ? r.data : []) } });
  const token = useWriteToken();
  return (
    <div className="space-y-4">
      <h1 className="font-heading text-xl font-semibold">Your repos</h1>
      <p className="max-w-3xl text-sm text-muted-foreground">
        Point cofabriq at a git clone on this machine. It copies it into its own folder, an agent reads how the repo
        installs and checks a change (its manifests, Makefile, CI) and proposes the commands, each with the line it
        came from. You read and edit them; nothing runs before you check. The check runs them in a sandbox, where
        only the install has network, and proves a test can fail. Then the repo is under Issues.
      </p>
      <ThreePlaces clone="<cofabriq>/data/clones/<owner>-<name>" />
      <AddRepo canWrite={token !== null} />
      {q.isPending ? (
        <Loading what="repos" />
      ) : (q.data ?? []).length > 0 ? (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Repo</TableHead>
              <TableHead>Setup</TableHead>
              <TableHead>Your folder</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(q.data ?? []).map((s) => {
              const [owner, name] = s.repo.split("/") as [string, string];
              return (
                <TableRow key={s.repo}>
                  <TableCell>
                    <Link to="/repos/$owner/$name" params={{ owner, name }} className="font-medium hover:underline">
                      {s.repo}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <PhaseBadge phase={s.phase} />
                  </TableCell>
                  <TableCell>
                    <code className="text-xs">{s.path}</code>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      ) : null}
    </div>
  );
}

function AddRepo({ canWrite }: { canWrite: boolean }) {
  const [path, setPath] = useState("");
  const client = useQueryClient();
  const navigate = useNavigate();
  const add = useRepoAdd({ fetch: writeAuth() });
  const send = () =>
    add.mutate(
      { data: { path: path.trim(), again: false, repair: false } },
      {
        onSuccess: async (r) => {
          if ((r.status as number) === 401) {
            dropToken(); // the server restarted with a new token
            return;
          }
          if (r.status === 200) {
            await client.invalidateQueries({ queryKey: getRepoSetupsQueryKey() });
            const [owner, name] = r.data.repo.split("/") as [string, string];
            await navigate({ to: "/repos/$owner/$name", params: { owner, name } });
          }
        },
      },
    );
  const r = add.data;
  const status: number | null = r && r.status !== 200 ? r.status : null;
  return (
    <div className="space-y-1 text-sm" role="form" aria-label="add a repo">
      <div className="flex flex-wrap items-center gap-2">
        <input
          className="w-[32rem] max-w-full rounded-md border px-3 py-1 font-mono text-xs"
          placeholder="/Users/you/code/myapp (the root of a git clone)"
          aria-label="path"
          value={path}
          onChange={(e) => setPath(e.target.value)}
        />
        <Button size="sm" onClick={send} disabled={!canWrite || !path.trim() || add.isPending}>
          {add.isPending && <LoaderCircle className="size-4 animate-spin" />}
          Add a repo
        </Button>
        <span className="text-muted-foreground">one agent session, about $0.10</span>
      </div>
      {!canWrite && (
        <p className="text-muted-foreground">
          Read-only here: open the URL that <code>cofabriq serve --write</code> printed to add a repo.
        </p>
      )}
      {status !== null && (
        <p className="text-destructive">
          {r?.status === 409
            ? r.data.detail
            : status === 401
              ? "The server restarted with a new token: open the new link it printed."
              : `Refused (${status}).`}
        </p>
      )}
    </div>
  );
}

/** One repo's setup: the proposal while it runs, then the file to read
 * and edit with each command's source, then the check, live. */
export function RepoPage() {
  const { owner, name } = useParams({ from: "/repos/$owner/$name" });
  const q = useRepoSetup(owner, name, {
    query: {
      select: (r) => (r.status === 200 ? r.data : null),
      refetchInterval: (query) => {
        const r = query.state.data;
        return r?.status === 200 && r.data.running ? POLL_MS : false;
      },
    },
  });
  if (q.isPending) return <Loading what="the repo" />;
  if (!q.data) return <Missing what="repo" />;
  return <Setup s={q.data} />;
}

function Setup({ s }: { s: RepoSetup }) {
  const token = useWriteToken();
  const [owner, name] = s.repo.split("/") as [string, string];
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-heading text-xl font-semibold">{s.repo}</h1>
        <PhaseBadge phase={s.phase} />
        <Link to="/repos" className="ml-auto text-sm text-muted-foreground hover:underline">
          ← Your repos
        </Link>
      </div>
      <ThreePlaces folder={s.path} clone={s.clone} />
      {s.error && <p className="text-sm text-destructive">The last step stopped: {s.error}</p>}
      {s.phase === "proposing" && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <LoaderCircle className="size-4 animate-spin" />
          Copying your clone, then an agent reads how it builds in cofabriq's copy (a minute or two). It changes
          nothing and runs nothing.
        </p>
      )}
      {s.phase === "proven" && (
        <div className="rounded-md border border-primary bg-primary/5 p-3 text-sm" role="status">
          <p className="font-medium">Proven: the install, every check, and a test that can fail.</p>
          <p>
            Flows can run on it now.{" "}
            <Link to="/issues" search={{ repo: s.repo }} className="font-medium underline">
              Write a ticket or import issues under Issues →
            </Link>
          </p>
        </div>
      )}
      {s.steps.length > 0 && <Steps steps={s.steps} running={s.phase === "checking"} />}
      {s.phase === "failed" && <Repair s={s} canWrite={token !== null && !s.running} />}
      {s.config && (
        <Editor key={s.config} s={s} owner={owner} name={name} canWrite={token !== null && !s.running} />
      )}
    </div>
  );
}

function Steps({ steps, running }: { steps: SetupStep[]; running: boolean }) {
  return (
    <Section title={running ? "The check, as it runs" : "The last check"}>
      <ul className="space-y-1 text-sm" data-testid="check-steps">
        {steps.map((st) => (
          <li key={st.name} className="space-y-1">
            <span className="flex items-center gap-2">
              {st.ok === null ? (
                <LoaderCircle className="size-4 animate-spin text-muted-foreground" />
              ) : st.ok ? (
                <CircleCheck className="size-4 text-emerald-700" />
              ) : (
                <CircleX className="size-4 text-destructive" />
              )}
              <span>{stepLabel(st.name)}</span>
            </span>
            {st.ok === false && st.output && <Pre text={st.output} className="ml-6 max-h-60" />}
          </li>
        ))}
      </ul>
    </Section>
  );
}

/** A failed check, back to the agent: it reads the failure and the file
 * as checked, and proposes the whole file again. Twice at most, then the
 * file is the dev's to fix. */
function Repair({ s, canWrite }: { s: RepoSetup; canWrite: boolean }) {
  const [owner, name] = s.repo.split("/") as [string, string];
  const client = useQueryClient();
  const add = useRepoAdd({ fetch: writeAuth() });
  const send = () =>
    add.mutate(
      { data: { path: s.path, again: false, repair: true } },
      {
        onSuccess: async (r) => {
          if ((r.status as number) === 401) {
            dropToken();
            return;
          }
          await client.invalidateQueries({ queryKey: getRepoSetupQueryKey(owner, name) });
        },
      },
    );
  const r = add.data;
  return (
    <div className="space-y-1 text-sm">
      <div className="flex flex-wrap items-center gap-3">
        <Button
          size="sm"
          variant="outline"
          onClick={send}
          disabled={!canWrite || !s.path || s.repairs_left <= 0 || add.isPending}
        >
          {add.isPending && <LoaderCircle className="size-4 animate-spin" />}
          Propose again with this failure
        </Button>
        <span className="text-muted-foreground">
          {s.repairs_left > 0
            ? `The agent reads what failed and the file as checked, then proposes it again (${s.repairs_left} of 2 left, about $0.10). Or fix the file below yourself.`
            : "Proposed again twice: fix the file below yourself."}
        </span>
      </div>
      {r?.status === 409 && <p className="text-destructive">{r.data.detail}</p>}
    </div>
  );
}

function stepLabel(name: string): string {
  if (name === "install") return "install (the only step with network)";
  if (name.startsWith("canary ")) return `${name}: a test written to fail fails, one written to pass passes`;
  return `check ${name}`;
}

/** The target file as proposed (or last saved), editable; Check saves it
 * and proves it. Each command's source next to it: the dev reads the
 * unchecked ones first. */
function Editor({ s, owner, name, canWrite }: { s: RepoSetup; owner: string; name: string; canWrite: boolean }) {
  const [text, setText] = useState(s.config);
  const client = useQueryClient();
  const check = useRepoCheck({ fetch: writeAuth() });
  const send = () =>
    check.mutate(
      { owner, name, data: { config: text } },
      {
        onSuccess: async (r) => {
          if ((r.status as number) === 401) {
            dropToken();
            return;
          }
          await Promise.all([
            client.invalidateQueries({ queryKey: getRepoSetupQueryKey(owner, name) }),
            client.invalidateQueries({ queryKey: getReposQueryKey() }),
          ]);
        },
      },
    );
  const r = check.data;
  const status: number | null = r && r.status !== 200 ? r.status : null;
  const unchecked = s.citations.filter((c) => !c.found).length;
  return (
    <div className="space-y-4">
      {s.citations.length > 0 && (
        <Section
          title="Where each command comes from"
          aside={
            <span className="text-xs text-muted-foreground">
              {s.citations.length - unchecked} of {s.citations.length} quoted from the repo
              {s.cost_usd > 0 && `, proposed for ${usd(s.cost_usd)}`}
            </span>
          }
        >
          <Citations items={s.citations} />
        </Section>
      )}
      {s.notes && (
        <Section title="The agent's notes">
          <Pre text={s.notes} />
        </Section>
      )}
      <Section title="The target file" aside={<code className="text-xs text-muted-foreground">targets/own/</code>}>
        <textarea
          aria-label="target file"
          className="h-96 w-full rounded-md border p-3 font-mono text-xs"
          spellCheck={false}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        {status !== null && (
          <p className="text-sm text-destructive">
            {r?.status === 409
              ? r.data.detail
              : status === 401
                ? "The server restarted with a new token: open the new link it printed."
                : `Refused (${status}).`}
          </p>
        )}
        <div className="flex items-center gap-3 text-sm">
          <Button size="sm" onClick={send} disabled={!canWrite || check.isPending}>
            {check.isPending && <LoaderCircle className="size-4 animate-spin" />}
            {text === s.config ? "Check" : "Save and check"}
          </Button>
          <span className="text-muted-foreground">
            Free, minutes: in a sandbox on cofabriq's copy. Editing the file later takes the proof back.
          </span>
        </div>
      </Section>
    </div>
  );
}

function Citations({ items }: { items: SetupCitation[] }) {
  return (
    <Table>
      <TableBody>
        {items.map((c) => (
          <TableRow key={c.field}>
            <TableCell className="w-28 align-top text-muted-foreground">{c.field}</TableCell>
            <TableCell className="align-top">
              <code className="text-xs break-all">{c.value}</code>
            </TableCell>
            <TableCell className="align-top text-xs">
              {c.found ? (
                <span>
                  <Badge variant="outline">quoted</Badge> <code>{c.source}</code>: <code>{c.quote}</code>
                </span>
              ) : (
                <span>
                  <Badge variant="destructive">unchecked</Badge>{" "}
                  {c.source ? (
                    <>
                      not found in <code>{c.source}</code>: read it
                    </>
                  ) : (
                    "no file says it: read it"
                  )}
                </span>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
