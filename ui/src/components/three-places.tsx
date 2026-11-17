/** Where cofabriq reads, where it works, and what it never touches: the
 * three places, said where the dev decides to trust it (adding a repo,
 * taking a result). `folder`: the dev's clone, when cofabriq knows it. */
export function ThreePlaces({ folder, clone }: { folder?: string; clone: string }) {
  const rows: [string, string][] = [
    [folder || "your folder", "your folder: cofabriq only reads it, and never writes to it"],
    [clone, "cofabriq's own copy: the agent works here, its commands run in a sandbox"],
    ["GitHub", "never written; read only to import issues, with a read-only token"],
  ];
  return (
    <div className="rounded-md border bg-muted/40 p-3 text-xs" data-testid="three-places">
      <div className="grid grid-cols-[minmax(0,auto)_1fr] gap-x-6 gap-y-1">
        {rows.map(([where, what]) => (
          <div key={what} className="contents">
            <code className="truncate" title={where}>
              {where}
            </code>
            <span className="text-muted-foreground">{what}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
