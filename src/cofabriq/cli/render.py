"""Terminal output of the triage card, the spec, the loop's result, the
review gate and the draft PR."""

import typer
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from cofabriq.eval import loop as loop_eval
from cofabriq.issues import triage as triage_mod
from cofabriq.loop import gate as gate_mod
from cofabriq.loop import pr as pr_mod
from cofabriq.loop import refine as refine_mod
from cofabriq.loop import run as run_mod


def show_card(card: triage_mod.TriageCard) -> None:
    """The card for a human: a label column and a value column, long values
    wrapped at word boundaries under their value. Issue and LLM text goes in as
    `Text`, which rich never parses as markup (`[red]` in an issue body prints
    as text). Piped, lines are not wrapped at all."""
    out = Console(highlight=False)
    if out.is_terminal:
        _render_card(out, card)
        return
    # Piped: no wrapping, and no grid padding at the end of lines.
    wide = Console(highlight=False, width=10_000)
    with wide.capture() as captured:
        _render_card(wide, card)
    typer.echo("\n".join(line.rstrip() for line in captured.get().splitlines()))


def _render_card(out: Console, card: triage_mod.TriageCard) -> None:
    i, d, r = card.issue, card.duplicate, card.run
    rows: list[tuple[str, Text]] = []

    def row(label: str, value: str, style: str = "bold") -> None:
        rows.append((label, Text(value, style=style)))

    def flush() -> None:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", width=13, no_wrap=True)
        grid.add_column()
        for label, value in rows:
            grid.add_row(label, value)
        out.print(Padding(grid, (0, 0, 0, 2), expand=False))
        rows.clear()

    out.print()
    out.print(Text.assemble((f"{i.repo}#{i.number}", "bold"), f" ({i.state})"))
    out.print(Padding(Text(i.url, style="dim"), (0, 0, 0, 2), expand=False))
    out.print(Padding(Text(card.summary), (0, 0, 0, 2), expand=False))
    row("type", card.type)
    if card.severity:
        row("severity", card.severity)
    row("component", card.component or "-")
    for n, step in enumerate(card.repro_steps, 1):
        row("repro" if n == 1 else "", f"{n}. {step}", "default")
    if card.missing:
        row("missing", ", ".join(card.missing), "yellow")
    for n, q in enumerate(card.missing_info):
        row("questions" if n == 0 else "", q, "default")
    target = f" #{d.target}" if d.target else ""
    style = {"duplicate_of": "bold yellow", "unsure": "yellow", "related": "cyan"}.get(
        d.verdict, "dim"
    )
    row("duplicate", f"{d.verdict}{target}", style)
    if d.evidence:
        row("", f'"{d.evidence}"', "italic")
    ready = card.readiness
    style = {"ready": "bold green", "needs_info": "bold yellow"}.get(
        ready.route, "bold"
    )
    row("route", ready.route, style)
    for reason in ready.reasons:
        row("", reason, "default")
    for n, line in enumerate((card.reply or "").splitlines()):
        row("reply draft" if n == 0 else "", line, "italic")
    flush()
    # Two separate searches (issues, doc sections); scores are cosine
    # similarity, not probabilities.
    out.print(Text("  related issues (similarity)", style="dim"))
    for x in card.related_issues:
        row("", f"{x.score:.2f}  #{x.number}", "default")
    if not card.related_issues:
        row("", "-", "default")
    flush()
    out.print(Text("  related docs (similarity)", style="dim"))
    clone = i.repo.split("/")[1] + "/"  # docs are keyed by the clone's folder
    for doc in card.related_docs:
        source = doc.source.removeprefix(clone)
        # A long heading trail is cut at the edge, not wrapped.
        rows.append(
            ("", Text(f"{doc.score:.2f}  {source}", no_wrap=True, overflow="ellipsis"))
        )
    if not card.related_docs:
        row("", "-", "default")
    flush()
    for reason in card.dropped:
        row("dropped", reason, "red")
    for reason in card.conflicts:
        row("conflict", reason, "yellow")
    row("run", f"${r.cost_usd:.4f}, {r.latency_ms / 1000:.1f} s, {r.model}", "dim")
    flush()


def show_spec(refined: refine_mod.Refined) -> None:
    """The spec for the dev at the plan gate. Agent text goes in as `Text`
    (never parsed as markup). Piped, lines are not wrapped (as the card)."""
    out = Console(highlight=False)
    if out.is_terminal:
        _render_spec(out, refined)
        return
    wide = Console(highlight=False, width=10_000)
    with wide.capture() as captured:
        _render_spec(wide, refined)
    typer.echo("\n".join(line.rstrip() for line in captured.get().splitlines()))


def _render_spec(out: Console, refined: refine_mod.Refined) -> None:
    s = refined.spec

    def section(title: str, lines: list[str]) -> None:
        out.print(Text(title, style="bold"))
        for line in lines or ["(none)"]:
            out.print(Padding(Text(line), (0, 0, 0, 2)))

    out.print(
        Text(
            f"{refined.repo}#{refined.number} at {refined.commit[:7]}, "
            f"{refined.route}, size {s.size}",
            style="bold",
        )
    )
    section("problem", [s.problem])
    section("files", [f"{f.path}: {f.why}" for f in s.files])
    section("tests", [f"{f.path}: {f.why}" for f in s.tests])
    if refined.route == "feature":
        criteria = [f"{n}. {c}" for n, c in enumerate(s.criteria, 1)]
        section("acceptance criteria", criteria)
    section("plan", [f"{n}. {step}" for n, step in enumerate(s.plan, 1)])
    section("assumptions", s.assumptions)
    section("open questions", s.open_questions)


def _indented(out: Console, text: Text) -> None:
    """Agent text under a trace line, wrapped: on a `soft_wrap` console
    `Padding` would crop it at the terminal width."""
    out.print(Padding(text, (0, 0, 0, 9)), soft_wrap=False)


def show_gate(gated: gate_mod.Gated) -> None:
    """Per review round: findings proven or not, the fix against them."""
    out = Console(highlight=False, soft_wrap=True)
    for v in gated.sentinel:  # the target's invariant: blocked, no review
        out.print(Text(f"blocked by the sentinel: {v}"))
    for n, r in enumerate(gated.rounds, 1):
        verdict = "approved" if r.review.approved else "changes requested"
        out.print(Text(f"review {n} {verdict}, {len(r.proofs)} to prove"))
        for p in r.proofs:
            state = f"proven by {', '.join(p.tests)}" if p.proven else "unproven"
            where = gate_mod.finding_text(p.finding)
            _indented(out, Text(f"{state}: {where}"))
        if r.attempts:
            ok = "green" if r.attempts[-1].green else "still red"
            _indented(out, Text(f"fix: {ok} after {len(r.attempts)}"))
    if gated.error:
        out.print(Text(f"review failed: {gated.error}", style="yellow"))
    blocked = gated.status == "blocked"
    note = " (a proven finding is left: no PR)" if blocked else ""
    line = f"gate     {gated.status}{note}, ${gated.cost_usd:.3f}"
    out.print(Text(line, style="red" if blocked else ""))


def show_draft(draft: pr_mod.Draft) -> None:
    """Tier and review verdict; agent text as `Text`."""
    out = Console(highlight=False, soft_wrap=True)
    tiers = ", ".join(f"{f.path} ({f.component}, {f.tier})" for f in draft.risk.files)
    out.print(Text(f"risk     {draft.risk.tier}: {tiers}"))
    review = draft.review
    if review is None:
        out.print(Text("review   did not run", style="yellow"))
        return
    r, run = review.reviewed, review.run
    verdict = "approved" if review.approved else "CHANGES REQUESTED"
    cost = f"${run.cost_usd:.3f} {run.turns} turns {run.latency_ms / 1000:.0f} s"
    out.print(Text(f"review   {verdict} ({cost})"))
    _indented(out, Text(r.summary))
    for f in r.findings:
        where = f.path if f.line is None else f"{f.path}:{f.line}"
        _indented(out, Text(f"{f.severity} {where}: {f.text}"))


def show_loop(judged: loop_eval.Judged) -> None:
    """The diff, then the trace per node and the verdicts: the verdicts end
    the output, where the eye lands. Agent text goes in as `Text`."""
    result = judged.result
    out = Console(highlight=False, soft_wrap=True)

    def attempts(node: str, items: list[run_mod.Attempt]) -> None:
        for a in items:
            run = a.run
            agent = (
                f"${run.cost_usd:.3f} {run.turns} turns {run.latency_ms / 1000:.0f} s"
                if run is not None
                else f"failed: {a.error}"
            )
            checks = " ".join(
                f"{c.name}:{'ok' if c.ok else 'FAIL'}({c.seconds:.0f}s)"
                for c in a.checks
            )
            out.print(Text(f"{node} {a.n}  agent {agent} | {checks}"))
            if a.reverted:
                reverted = ", ".join(a.reverted)
                out.print(Text(f"         reverted: {reverted}", style="yellow"))
            if a.summary:
                _indented(out, Text(a.summary))

    if result.diff:
        out.print(Text(result.diff))
    out.print(Text(f"setup    {result.setup_s:.0f} s (worktree, install)"))
    attempts("repro", result.reproduce)
    if result.red is not None:
        state = "red" if not result.red.ok else "green before any change"
        source = "planted" if result.planted else "the agent's"
        line = f"{source} {', '.join(result.repro)}: {state}"
        out.print(Text(f"repro    {line}, {result.red.seconds:.0f} s"))
    attempts("fix", result.attempts)
    out.print(Text(f"{result.status}: {', '.join(result.changed) or 'no changes'}"))
    for name, check, meaning in [
        ("judge", judged.judge, "the planted repro on the fix"),
        ("valid", judged.valid, "the agent's repro with the reference fix"),
    ]:
        if check is not None:
            verdict = "green" if check.ok else "RED"
            out.print(Text(f"{name}    {verdict} ({meaning})"))
