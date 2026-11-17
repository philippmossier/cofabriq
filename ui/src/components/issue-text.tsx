import { ExternalLink } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

/** An issue as its reporter wrote it: untrusted, so plain text, never
 * rendered markdown or HTML. Three lines, then "show more" when it is
 * longer. */
export function IssueText({ body, className }: { body: string; className?: string }) {
  const box = useRef<HTMLDivElement>(null);
  const [long, setLong] = useState(false);
  const [open, setOpen] = useState(false);
  useLayoutEffect(() => {
    const el = box.current;
    if (el && !open) setLong(el.scrollHeight > el.clientHeight + 1);
  }, [body, open]);
  return (
    <div className={cn("rounded-md border bg-muted/40 px-3 py-2", className)}>
      <div
        ref={box}
        className={cn("text-sm leading-relaxed whitespace-pre-wrap", !open && "line-clamp-3")}
        data-testid="issue-text"
      >
        {body.trim() || <span className="text-muted-foreground">(no description)</span>}
      </div>
      {(long || open) && (
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="mt-1 text-xs text-muted-foreground hover:text-foreground"
        >
          {open ? "show less" : "show more"}
        </button>
      )}
    </div>
  );
}

/** The issue on GitHub, as a link on a title's line. */
/** The issue on GitHub; null: a local ticket, which has no page there. */
export function GitHubLink({ url }: { url: string | null }) {
  if (url === null) return <span className="text-xs text-muted-foreground">local ticket, not on GitHub</span>;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
    >
      GitHub <ExternalLink className="size-3" />
    </a>
  );
}
