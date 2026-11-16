import { ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";

/** An issue as its reporter wrote it: untrusted, so plain text, never
 * rendered markdown or HTML. */
export function IssueText({ body, url, className }: { body: string; url?: string; className?: string }) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <div
        className="max-h-72 overflow-auto rounded-md border bg-muted/40 p-3 text-sm leading-relaxed whitespace-pre-wrap"
        data-testid="issue-text"
      >
        {body.trim() || <span className="text-muted-foreground">(no description)</span>}
      </div>
      {url && (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          on GitHub, with its comments <ExternalLink className="size-3" />
        </a>
      )}
    </div>
  );
}
