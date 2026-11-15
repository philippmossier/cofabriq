"""A run's steps for the viewer and `flow show --steps`: the lines
`tracing.RunStore` appended to `data/traces/<run>.jsonl`, folded into a
tree, each step with its title and doc from `docs/steps/` and a link to
its code.

    {"ev": "start", "span", "parent", "name", "at", "attrs"[, "commit"]}
    {"ev": "end", "span", "at", "ms", "status", "error", "attrs", "payload"}

A start without an end is a step still running; a node that paused at a
gate ends with `cofabriq.paused`. The roots are the run's flow invocations
in order: `flow start` up to the plan gate, then one per answered gate.
The fold is the contract the API serves, not the lines (ADR-009).
"""

import json
import re
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from cofabriq import tracing

SOURCE = "https://github.com/philippmossier/cofabriq"  # code links point here
DOCS = tracing.ROOT / "docs" / "steps"
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID = re.compile(r"^[0-9a-f]{16}$")

type Value = str | int | float | bool | list[str | int | float | bool]
type Status = Literal["running", "ok", "error", "paused"]


class Code(BaseModel):
    """The function a step stands for."""

    function: str = Field(
        description="Module-qualified: `cofabriq.loop.run.build_stage`."
    )
    file: str = Field(description="Relative to the checkout.")
    line: int
    url: str = Field(
        description="A permalink at the commit that ran; empty without a commit."
    )


class RunStep(BaseModel):
    """One step of a run (a span), with the steps it ran inside it."""

    id: str = Field(
        description="The span id: its payload is /api/traces/{run}/payloads/{id}."
    )
    name: str = Field(description="The span's name: `node build`, `sandbox.run`.")
    doc: str = Field(description="The step doc's key in /api/step-docs: `build`.")
    title: str = Field(description="The doc's title, or the span's name without one.")
    detail: str = Field(
        description="What this one did: the command, the model, a gate's pause."
    )
    started: datetime
    ms: int | None = Field(description="Its duration; null while it runs.")
    status: Status = Field(
        description="`paused`: it stopped at a gate. A red sandbox command the "
        "reproducer wanted is `ok`, with `cofabriq.sandbox.ok` false."
    )
    error: str = Field(description="The exception's message when `error`.")
    attrs: dict[str, Value] = Field(
        description="The span's attributes: model, turns, cost, command."
    )
    code: Code | None
    payload: bool = Field(
        description="What it got and gave was kept: prompt, answer, output, state."
    )
    children: list["RunStep"] = Field(description="The steps it ran, by start time.")


class StepDoc(BaseModel):
    """What a kind of step is for, from `docs/steps/<key>.md`."""

    key: str
    title: str = Field(description="Line 1 of the doc.")
    summary: str = Field(description="The plain sentence under the title.")
    why: str = Field(
        description="Markdown: why the step exists, its decision, alternatives."
    )
    code: str = Field(description="The module-qualified function it describes.")


def doc_key(name: str) -> str:
    """The step doc a span name belongs to."""
    if name.startswith("node "):
        return name.removeprefix("node ")
    if name == tracing.FLOW:
        return "flow"
    for prefix, key in (("invoke_agent ", "agent"), ("chat ", "chat")):
        if name.startswith(prefix):
            return key
    if name.startswith("embeddings "):
        return "embeddings"
    return "sandbox" if name == "sandbox.run" else name


def _detail(name: str, attrs: dict[str, Value]) -> str:
    if name == "sandbox.run":
        net = "network" if attrs.get("cofabriq.sandbox.network") else "no network"
        return f"{attrs.get('cofabriq.sandbox.command', '')} ({net})"
    if name.startswith(("invoke_agent ", "chat ", "embeddings ")):
        model = attrs.get(tracing.RESPONSE_MODEL) or attrs.get(tracing.REQUEST_MODEL)
        what = attrs.get(tracing.WHAT)
        return f"{model}{f', {what}' if what else ''}"
    if name == tracing.FLOW:
        return "resumed with your answer" if attrs.get("cofabriq.resume") else "started"
    if (approved := attrs.get(tracing.APPROVED)) is not None:
        return "you approved" if approved else "you rejected"
    return "paused for your answer" if attrs.get("cofabriq.paused") else ""


def load_docs(folder: Path = DOCS) -> dict[str, StepDoc]:
    """`<key>.md`: `# Title`, a plain sentence, then `## Why` and `## Code`
    (one backticked function)."""
    docs: dict[str, StepDoc] = {}
    for path in sorted(folder.glob("*.md")):
        text = path.read_text("utf-8")
        head, _, rest = text.partition("\n## Why\n")
        why, _, code = rest.partition("\n## Code\n")
        title, _, summary = head.strip().partition("\n")
        symbol = re.search(r"`([\w.]+)`", code)
        docs[path.stem] = StepDoc(
            key=path.stem,
            title=title.removeprefix("# ").strip(),
            summary=" ".join(summary.split()),
            why=why.strip(),
            code=symbol.group(1) if symbol else "",
        )
    return docs


def load_steps(
    folder: Path, run: str, docs: dict[str, StepDoc]
) -> list[RunStep] | None:
    """The run's step tree, roots in order; None when it has no file."""
    path = folder / f"{run}.jsonl"
    if not RUN_ID.match(run) or not path.is_file():
        return None
    starts: dict[str, dict[str, object]] = {}
    ends: dict[str, dict[str, object]] = {}
    for raw in path.read_text("utf-8").splitlines():
        try:  # a line half written by another process is skipped
            line: dict[str, object] = json.loads(raw)
        except json.JSONDecodeError:
            continue
        (starts if line.get("ev") == "start" else ends)[str(line.get("span"))] = line
    steps = {sid: _step(start, ends.get(sid), docs) for sid, start in starts.items()}
    roots: list[RunStep] = []
    for sid, start in starts.items():
        parent = steps.get(str(start.get("parent")))
        (parent.children if parent is not None else roots).append(steps[sid])
    for root in roots:
        _link_code(root, str(starts[root.id].get("commit", "")))
    for step in steps.values():
        step.children.sort(key=lambda c: c.started)
    roots.sort(key=lambda r: r.started)
    _one_row_per_gate(roots)
    return roots


def _one_row_per_gate(roots: list[RunStep]) -> None:
    """A gate runs twice: it pauses at the end of one invocation and runs
    again, with the answer, at the start of the next. Once answered, the
    paused copy says nothing the answered one doesn't: it goes."""
    for done, resumed in pairwise(roots):
        if not done.children or not resumed.children:
            continue
        paused = done.children[-1]
        if paused.status == "paused" and resumed.children[0].name == paused.name:
            done.children.pop()


def _step(
    start: dict[str, object], end: dict[str, object] | None, docs: dict[str, StepDoc]
) -> RunStep:
    name = str(start["name"])
    attrs = _attrs((end or start).get("attrs"))
    key = doc_key(name)
    status: Status = "running"
    if end is not None:
        status = "error" if end.get("status") == "error" else "ok"
        status = "paused" if attrs.get("cofabriq.paused") else status
    found = docs.get(key)
    return RunStep(
        id=str(start["span"]),
        name=name,
        doc=key,
        title=found.title if found else name,
        detail=_detail(name, attrs),
        started=datetime.fromisoformat(str(start["at"])),
        ms=int(str(end["ms"])) if end is not None else None,
        status=status,
        error=str(end.get("error", "")) if end is not None else "",
        attrs=attrs,
        code=None,
        payload=bool(end.get("payload")) if end is not None else False,
        children=[],
    )


def _attrs(raw: object) -> dict[str, Value]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Value] = {}
    for k, v in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if isinstance(v, str | int | float | bool):
            out[str(k)] = v  # pyright: ignore[reportUnknownArgumentType]
        elif isinstance(v, list):
            out[str(k)] = [str(x) for x in v]  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
    return out


def _link_code(step: RunStep, commit: str) -> None:
    """Each step's code, linked at the commit its process ran."""
    a = step.attrs
    function, file = a.get(tracing.CODE_FUNCTION), a.get(tracing.CODE_FILE)
    line = a.get(tracing.CODE_LINE)
    if isinstance(function, str) and isinstance(file, str) and isinstance(line, int):
        url = f"{SOURCE}/blob/{commit}/{file}#L{line}" if commit else ""
        step.code = Code(function=function, file=file, line=line, url=url)
    for child in step.children:
        _link_code(child, commit)


def load_payload(folder: Path, run: str, span: str) -> dict[str, object] | None:
    """What one step got and gave; None when nothing was kept."""
    if not RUN_ID.match(run) or not SPAN_ID.match(span):
        return None
    path = folder / run / f"{span}.json"
    return json.loads(path.read_bytes()) if path.is_file() else None


class Totals(BaseModel):
    seconds: float = Field(description="First start to last end, gates included.")
    agents: int = Field(description="Agent sessions run.")
    turns: int = Field(description="Their turns, summed.")
    cost_usd: float = Field(description="At API list prices.")


def totals(roots: list[RunStep]) -> Totals:
    flat = [s for r in roots for s in _walk(r)]
    agents = [s for s in flat if s.doc == "agent"]
    ends = [s.started.timestamp() + (s.ms or 0) / 1000 for s in roots]
    first = roots[0].started.timestamp() if roots else 0.0
    return Totals(
        seconds=round(max(ends, default=first) - first, 1),
        agents=len(agents),
        turns=sum(int(str(s.attrs.get(tracing.TURNS, 0))) for s in agents),
        cost_usd=round(
            sum(float(str(s.attrs.get(tracing.COST, 0.0))) for s in flat), 4
        ),
    )


def _walk(step: RunStep) -> list[RunStep]:
    return [step, *(s for c in step.children for s in _walk(c))]
