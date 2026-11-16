import { type ReactNode, useState } from "react";
import type { Check } from "@/api/generated";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** A titled block of the spec or trace. `check`: at a gate, what to look
 * for in it, said where it is read rather than in a list up front.
 * `aside`: a small link or note on the title's line. */
export function Section({
  title,
  check,
  aside,
  children,
}: {
  title: string;
  check?: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="space-y-2">
      <div>
        <div className="flex items-baseline gap-3">
          <h3 className="text-sm font-medium text-muted-foreground">{title}</h3>
          {aside}
        </div>
        {check && <p className="text-xs text-primary">Check: {check}</p>}
      </div>
      {children}
    </section>
  );
}

/** Copies `text` to the clipboard (127.0.0.1 is a secure context). */
export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () =>
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  return (
    <Button size="sm" variant="outline" onClick={copy}>
      {copied ? "Copied" : label}
    </Button>
  );
}

/** A section folded away: there to check, not to read first. */
export function Fold({ title, children, open = false }: { title: string; children: ReactNode; open?: boolean }) {
  return (
    <details open={open} className="group rounded-md border px-3 py-2">
      <summary className="cursor-pointer text-sm font-medium text-muted-foreground select-none group-open:mb-3">
        {title}
      </summary>
      {children}
    </details>
  );
}

export function List({ items, ordered = false }: { items: string[]; ordered?: boolean }) {
  if (items.length === 0) return <p className="text-sm text-muted-foreground">(none)</p>;
  const Tag = ordered ? "ol" : "ul";
  return (
    <Tag className={cn("space-y-1 pl-5 text-sm", ordered ? "list-decimal" : "list-disc")}>
      {items.map((item, i) => (
        <li key={i}>{item}</li>
      ))}
    </Tag>
  );
}

/** Agent text and check output: shown as plain text, never as HTML. */
export function Pre({ text, className }: { text: string; className?: string }) {
  return (
    <pre
      className={cn(
        "max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap",
        className,
      )}
    >
      {text}
    </pre>
  );
}

const GOOD = new Set(["green", "approved", "ready"]);
const BAD = new Set(["escalated", "blocked", "setup_failed", "no_repro", "not_red", "test_rejected"]);

export function Status({ value }: { value: string }) {
  const variant = GOOD.has(value) ? "default" : BAD.has(value) ? "destructive" : "secondary";
  return <Badge variant={variant}>{value.replace("_", " ")}</Badge>;
}

/** A yes / no / not-measured verdict (judge, validity). */
export function Verdict({ ok, yes, no }: { ok: boolean | null | undefined; yes: string; no: string }) {
  if (ok === null || ok === undefined) return <span className="text-muted-foreground">-</span>;
  return <Badge variant={ok ? "outline" : "destructive"}>{ok ? yes : no}</Badge>;
}

/** "red" is the check that the test fails before the change: ok means it did. */
function label(c: Check): string {
  if (c.name === "red") return c.ok ? "fails before ✓" : "passes before ✗";
  return `${c.name} ${c.ok ? "ok" : "red"}`;
}

export function Checks({ checks }: { checks: Check[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {checks.map((c, i) => (
        <Badge key={i} variant={c.ok ? "outline" : "destructive"} title={`${c.seconds}s`}>
          {label(c)}
        </Badge>
      ))}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <p className="text-sm text-muted-foreground">Loading {what}...</p>;
}

export function Missing({ what }: { what: string }) {
  return <p className="text-sm text-destructive">No such {what}.</p>;
}
