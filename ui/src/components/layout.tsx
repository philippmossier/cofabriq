import { Link, Outlet } from "@tanstack/react-router";

export function Layout() {
  const tab = "rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground";
  const active = { className: "bg-muted text-foreground" };
  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <header className="mb-6 flex items-center gap-6">
        <span className="font-heading text-lg font-semibold">cofabriq</span>
        <nav className="flex gap-1">
          <Link to="/specs" className={tab} activeProps={active}>
            Specs
          </Link>
          <Link to="/runs" className={tab} activeProps={active}>
            Runs
          </Link>
        </nav>
        <span className="ml-auto text-xs text-muted-foreground">read-only</span>
      </header>
      <Outlet />
    </div>
  );
}
