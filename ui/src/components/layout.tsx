import { Link, Outlet } from "@tanstack/react-router";
import { useEffect } from "react";
import { useFlowList } from "@/api/generated";
import { useWriteToken } from "@/lib/token";

const POLL_MS = 3000;

export function Layout() {
  const token = useWriteToken();
  const waiting = useWaiting(token !== null);
  const tab = "rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground";
  const active = { className: "bg-muted text-foreground" };
  return (
    <div className="mx-auto max-w-6xl px-6 py-5">
      <header className="mb-3 flex items-center gap-6">
        <span className="font-heading text-lg font-semibold">cofabriq</span>
        <nav className="flex gap-1">
          <Link to="/issues" className={tab} activeProps={active}>
            Issues
          </Link>
          <Link to="/flows" className={tab} activeProps={active}>
            Flows
            {waiting > 0 && (
              <span
                className="ml-1.5 rounded-full bg-primary px-1.5 py-px text-xs text-primary-foreground"
                title={`${waiting} waiting for your answer`}
              >
                {waiting}
              </span>
            )}
          </Link>
          <Link to="/repos" className={tab} activeProps={active}>
            Repos
          </Link>
          <Link to="/runs" className={tab} activeProps={active}>
            Runs
          </Link>
          <Link to="/evals" search={{ tab: "eval" }} className={tab} activeProps={active}>
            Evals
          </Link>
        </nav>
        {/* only the state that explains disabled buttons is worth the room */}
        {!token && (
          <span
            className="ml-auto text-xs text-muted-foreground"
            title="Open the URL that `cofabriq serve --write` printed to start flows and answer gates."
          >
            read-only
          </span>
        )}
      </header>
      <Outlet />
    </div>
  );
}

/** Flows paused at a gate; the count goes into the tab's title too, so a
 * run that took minutes says so from a background tab. */
function useWaiting(polling: boolean): number {
  const q = useFlowList({
    query: {
      select: (r) => (r.status === 200 ? r.data.filter((f) => f.waiting && !f.running).length : 0),
      refetchInterval: polling ? POLL_MS : false,
    },
  });
  const n = q.data ?? 0;
  useEffect(() => {
    document.title = n > 0 ? `(${n}) waiting · cofabriq` : "cofabriq";
  }, [n]);
  return n;
}
