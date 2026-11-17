"""`cofabriq serve`: an HTTP API over what refine and the loop stored, plus
the built UI.

    data/refine/*.json   specs: {key: Refined}
    data/runs/*.json     runs:  {key: Judged} (eval-, features-, wrong-),
                                [GateCase] (gate-), or one run record
                                (<owner>-<name>-<n>-<stamp>.json, `run`),
                                its change next to it (`.patch`)
    data/traces/         a flow's steps while and after it runs
                                (`runsteps.py`), what each step got and gave

The API returns its own response models, not the stored ones: a stored
shape can change without changing the UI's contract (ADR-009). Everything
is read per request from the files, nothing is cached. It binds to
127.0.0.1 only. Every route reads, except four: with `--write`, the browser
starts a flow and answers a paused flow's gate (`_flow_routes`), writes a
local ticket (`/api/tickets`) and imports open issues into the store
(`/api/repos/.../import`, read-only towards GitHub), behind the token and the
checks in `serve_guard`. File names in URLs must be files in the folder (no paths, no
`..`); a ticket in a URL is a key (`51`, `L3`), checked by its pattern.
"""

import importlib.metadata
import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

from cofabriq import git as git_mod
from cofabriq import runsteps, tracing
from cofabriq.eval.loop import Judged
from cofabriq.files import STAMP
from cofabriq.issues import tickets as tickets_mod
from cofabriq.issues import token as token_mod
from cofabriq.issues import triage as triage_mod
from cofabriq.issues.fetch import load_index, load_issues, store_open
from cofabriq.issues.github import GitHubError
from cofabriq.issues.model import (
    KEY,
    Issue,
    IssueSource,
    Source,
    check_repo,
    ticket_key,
)
from cofabriq.issues.triage import IssueType, Triager
from cofabriq.loop import repo_jobs
from cofabriq.loop.gate import Gated
from cofabriq.loop.graph import FlowRunner, FlowStatus, GateAnswer, Refused
from cofabriq.loop.handoff import Handoff, take_command
from cofabriq.loop.intake import not_ready
from cofabriq.loop.onboard import on_github, unproven
from cofabriq.loop.record import RunRecord
from cofabriq.loop.refine import FileChange, Refined, Route, Spec
from cofabriq.loop.run import Attempt, CheckResult, LoopResult, load_rules
from cofabriq.serve_guard import guard
from cofabriq.targets import clone_dir

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_AROUND = 8  # lines of code shown above and below the spec's line
_MAX_LINES = 80  # one snippet, however wide the range the spec names
_STAMP = re.compile(r"(\d{8}T\d{6})")

type RunKind = Literal["eval", "features", "wrong", "gate", "run"]
# a ticket in a path, `51` or `L3`: anything else is a 422 before the route runs
# (a plain alias: FastAPI reads `Annotated` metadata through it, not through `type`)
TicketKey = Annotated[str, PathParam(pattern=KEY, description="`51` or `L3`.")]
# a flow's parts as the browser shows them, in order (`_phase`)
type Phase = Literal["spec", "plan", "test", "test_gate", "build", "review", "done"]


# ---- the contract (what the UI sees) -----------------------------------------


Ticket = Annotated[
    str, Field(description="The ticket: `51` (GitHub's #51) or `L3` (a local one).")
]


class SpecSummary(BaseModel):
    file: str
    key: str
    repo: str
    number: int
    ticket: Ticket
    route: Literal["bug", "feature"]
    size: str
    files: int
    criteria: int
    open_questions: int
    not_ready: list[str] | None  # None: the clone isn't there to check paths
    made_at: datetime | None


class EditNote(BaseModel):
    """A place of the change, sketched by the agent; not run."""

    old: str = Field(description="The lines as they are, found in the file.")
    new: str = Field(description="What the agent proposes they become.")
    line: int = Field(description="Where `old` starts in the file.")


class FileNote(BaseModel):
    path: str
    why: str
    at: list[str] = Field(description='The lines it changes: "74", "78-83".')
    edits: list[EditNote] = Field(description="Sketches checked against the file.")


class CaseNote(BaseModel):
    name: str
    expect: str
    fails_now: bool = Field(description="Fails on the code as it is now: the bug.")


class ProofNote(BaseModel):
    """How the spec says a failing test will show it."""

    kind: Literal["unit", "browser", "proxy", "none"]
    how: str
    cases: list[CaseNote] = Field(
        description="The planned cases; empty for a feature and older specs."
    )


def _proof(spec: Spec | None) -> ProofNote | None:
    p = spec.proof if spec is not None else None
    if p is None:
        return None
    cases = [CaseNote(**c.model_dump()) for c in p.cases]
    return ProofNote(kind=p.kind, how=p.how, cases=cases)


def _note(f: FileChange) -> FileNote:
    return FileNote(
        path=f.path,
        why=f.why,
        at=f.at,
        edits=[EditNote(old=e.old, new=e.new, line=e.line) for e in f.edits],
    )


class SpecView(BaseModel):
    file: str
    key: str
    repo: str
    number: int
    ticket: Ticket
    commit: str
    route: Literal["bug", "feature"]
    size: str
    problem: str
    files: list[FileNote]
    tests: list[FileNote]
    proof: ProofNote | None
    criteria: list[str]
    plan: list[str]
    assumptions: list[str]
    open_questions: list[str]
    unknown_paths: list[str]
    not_ready: list[str] | None
    cost_usd: float
    turns: int
    seconds: float


class Check(BaseModel):
    name: str
    ok: bool
    seconds: float
    output: str


class Step(BaseModel):
    """One agent attempt: the reproducer's or the implementer's."""

    phase: Literal["test", "build", "fix"]  # fix: against proven findings
    n: int
    summary: str
    error: str
    reverted: list[str]
    checks: list[Check]
    cost_usd: float
    seconds: float


class FindingView(BaseModel):
    path: str
    line: int | None
    severity: str
    text: str


class ProofView(BaseModel):
    finding: FindingView
    proven: bool
    quote: str
    tests: list[str]
    red: Check | None


class ReviewRound(BaseModel):
    verdict: str
    approved: bool
    summary: str
    test_note: str
    cause_note: str
    findings: list[FindingView]
    proofs: list[ProofView]
    fix: list[Step]


class RunSummary(BaseModel):
    file: str
    key: str
    kind: RunKind
    repo: str
    number: int
    ticket: Ticket
    title: str  # the issue's, from the store; "" for an eval or one not stored
    status: str  # the loop's status, or the gate's when it ran
    judge: bool | None  # hidden repro / reference tests green
    valid: bool | None  # the agent's test right on the reference
    tries: int
    cost_usd: float
    made_at: datetime | None


class HandoffView(BaseModel):
    """The change to take: a branch in cofabriq's own copy of the repo and
    the same commits as a patch. Nothing was written to the dev's folder."""

    branch: str  # "" when git failed (`error`)
    commits: int  # the test, then the change
    clone: str  # cofabriq's copy the branch is in
    fetch: str = Field(
        description="Run in your own folder: fetch the branch from cofabriq's "
        "copy and switch to it; a branch of that name with commits of yours "
        "stays as it is."
    )
    patch_url: str  # the same commits for `git am`; "" when git failed
    error: str


class RunTrace(RunSummary):
    commit: str
    red: Check | None
    steps: list[Step]
    changed: list[str]
    diff: str
    gate: str | None  # approved | unproven | blocked; None: no gate ran
    rounds: list[ReviewRound]
    pr_body: str | None
    sentinel: list[str]  # the target's invariant broken: why it's blocked
    evidence: list[str]  # screenshot URLs of the browser test after the change
    trace: str  # its steps: /api/traces/<trace>; "" for a run before traces
    pr_url: str | None  # the draft PR `run --apply` opened; None: dry run
    handoff: HandoffView | None  # None: no draft (blocked, not reviewed, older run)


class FlowSummary(BaseModel):
    """A run in the browser gate: where it waits, or how it ended."""

    thread: str
    repo: str
    number: int
    ticket: Ticket
    url: str | None = Field(description="The issue on GitHub; null: a local ticket.")
    title: str
    route: Literal["bug", "feature"]
    waiting: Literal["plan", "test"] | None
    running: bool
    phase: Phase = Field(
        description="The part of the run it is in, waits at, or ended in."
    )
    # e.g. "approved", "escalated", "rejected: plan rejected", "cut off: ..."
    outcome: str
    error: str


class FlowView(FlowSummary):
    body: str = Field(
        description="The issue as it was read at the start: untrusted, show as text."
    )
    size: str | None
    problem: str
    criteria: list[str]
    files: list[FileNote]
    plan: list[str]
    assumptions: list[str]
    open_questions: list[str]
    diff: str  # the frozen test, at the test gate
    red: str  # its run on the unchanged code: how it fails now
    retests_left: int  # tests the dev may still send back at the test gate
    commit: str  # the spec's: its paths and code lines are at it; "" before refine
    tests: list[FileNote]
    proof: ProofNote | None
    unknown_paths: list[str]  # not in the repo at the commit: new files
    trace: str  # its steps so far: /api/traces/<trace>; "" untraced
    record: str  # the finished run: /api/runs/<record>/<ticket>; "" before


class CodeLines(BaseModel):
    """A few lines of one file at a commit, read from the clone: the plan
    gate's "current code" next to what the spec will change. The target's
    text: shown as text."""

    path: str
    start: int = Field(description="The first line's number, 1-based.")
    lines: list[str]
    total: int = Field(description="The file's length in lines.")


class StepTree(BaseModel):
    """A run's steps so far: the roots are its flow invocations in order
    (`flow start` up to the first gate, then one per answered gate)."""

    steps: list[runsteps.RunStep]
    totals: runsteps.Totals


class Repo(BaseModel):
    """A repo a flow can run on: `[loop]` in its target file, and stored
    issues (`fetch-issues`) or local tickets."""

    repo: str
    issues: int
    tickets: int = Field(description="Local tickets (L1, L2, ...).")
    on_github: bool = Field(
        description="Its issues are on github.com: they can be imported."
    )


class GitHubAccess(BaseModel):
    """The GitHub token cofabriq reads with, as the dev should know it
    (never the token): where it came from, and whether it could write."""

    source: Literal["env", "gh"] | None = Field(
        description="`env`: GITHUB_TOKEN; `gh`: `gh auth token`; null: none."
    )
    can_write: bool | None = Field(
        description="Its scopes can write; null: it lists none (fine-grained)."
    )
    warning: str


class ImportAsk(BaseModel):
    limit: int = Field(default=50, ge=1, le=100, description="The newest open ones.")
    confirm: bool = Field(
        default=False, description="Import even though more are open than `limit`."
    )


class Imported(BaseModel):
    total: int = Field(description="Open on GitHub.")
    written: int
    unchanged: int
    asked: bool = Field(
        description="More are open than `limit`: nothing imported, ask the dev."
    )


class IssueRow(BaseModel):
    """One stored issue or local ticket, as the Issues page lists it."""

    number: int
    ticket: Ticket
    source: Source
    title: str
    state: str = Field(description="`open` or `closed`, as last fetched.")
    labels: list[str]
    comments: int
    updated_at: datetime
    url: str | None = Field(description="The issue on GitHub; null: a local ticket.")
    flow: FlowSummary | None = Field(
        description="Its flow, where it waits or how it ended; null: none yet."
    )


class IssueView(IssueRow):
    """One stored issue with its text, to read before starting a flow."""

    body: str = Field(description="Untrusted: show it as text.")


class Start(BaseModel):
    route: Route = Field(description="`feature` writes acceptance criteria.")
    again: bool = Field(
        default=False,
        description="Replace the issue's ended flow; why it ended goes to refine.",
    )


class Started(BaseModel):
    thread: str


class Refusal(BaseModel):
    detail: str = Field(description="Why not: a blocker, a flow already there.")


class SetupCitation(BaseModel):
    """Where a proposed command came from: the repo file and the line in
    it, and whether that line is in that file (else: unchecked)."""

    field: str = Field(description="`image`, `install`, `check test`, ...")
    value: str = Field(description="The command as proposed. Untrusted.")
    source: str = Field(description="The repo file; empty: no file says it.")
    quote: str = Field(description="The line it came from. Untrusted.")
    found: bool


class SetupStep(BaseModel):
    name: str = Field(description="`install`, a check's name, `canary <suite>`.")
    ok: bool | None = Field(description="None: running.")
    output: str = Field(description="A failed step's output, its end. Untrusted.")


class RepoSetup(BaseModel):
    """Adding a repo, as far as it got: the proposed target file (edited by
    the dev), each command's source, and its check in the sandbox."""

    repo: str
    path: str = Field(description="The dev's clone, which cofabriq only reads.")
    clone: str = Field(description="cofabriq's own copy of it: the agent works here.")
    phase: repo_jobs.SetupPhase
    running: bool
    config: str = Field(description="The target file as saved. Untrusted.")
    citations: list[SetupCitation]
    notes: str = Field(description="The agent's notes. Untrusted.")
    cost_usd: float = Field(description="The proposal's agent run.")
    steps: list[SetupStep] = Field(description="The check's, live while it runs.")
    error: str = Field(description="The last job died before it could say more.")
    repairs_left: int = Field(
        description="How often it may still be proposed again with a failed check."
    )


class AddRepo(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=4096,
        description="The root of the dev's git clone, absolute (`~` allowed).",
    )
    again: bool = Field(
        default=False,
        description="Propose again for a repo already added: the file is replaced.",
    )
    repair: bool = Field(
        default=False,
        description="Propose again, told how the last check failed (its output, "
        "untrusted, fenced in the prompt). Only after a failed check, at most "
        "twice.",
    )


class CheckRepo(BaseModel):
    config: str = Field(
        min_length=1,
        max_length=100_000,
        description="The target file as the dev edited it: saved, then proven.",
    )


class SetupStarted(BaseModel):
    repo: str


class Answer(GateAnswer):
    # the gate's answer under the API's name; a longer reason is a 422 here
    reason: str = Field(default="", max_length=500)


class Answered(BaseModel):
    started: bool


class NewTicket(BaseModel):
    title: str = Field(min_length=1, max_length=tickets_mod.MAX_TITLE)
    body: str = Field(default="", max_length=tickets_mod.MAX_BODY)
    anyway: bool = Field(
        default=False, description="Save it even when the check finds gaps."
    )


class TicketSaved(BaseModel):
    """What the readiness read found, and the ticket if it was saved."""

    type: IssueType = Field(description="How the check reads it: bug, feature, ...")
    missing: list[str] = Field(description="What it lacks; empty: ready to run.")
    cost_usd: float
    saved: IssueRow | None = Field(
        description="The new ticket; null: not saved (gaps, and not `anyway`)."
    )


# ---- reading the files ------------------------------------------------------


def _stamp(file: str) -> datetime | None:
    m = _STAMP.search(file)
    return datetime.strptime(m.group(1), STAMP).replace(tzinfo=UTC) if m else None


def _files(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.json"), reverse=True) if folder.is_dir() else []


def _file(folder: Path, name: str) -> Path:
    """`name` as a file of `folder`, or 404: never a path from outside it."""
    if not _NAME.match(name):
        raise HTTPException(status_code=404, detail="no such file")
    path = folder / f"{name}.json"
    if path not in _files(folder):
        raise HTTPException(status_code=404, detail="no such file")
    return path


_object = TypeAdapter(dict[str, object])
_objects = TypeAdapter(list[dict[str, object]])
_json = TypeAdapter(dict[str, JsonValue])


def _load(path: Path) -> object:
    return json.loads(path.read_text("utf-8"))


def _as_dict(raw: object) -> dict[str, object]:
    try:
        return _object.validate_python(raw)
    except ValidationError:
        return {}


def _specs_in(path: Path) -> dict[str, Refined]:
    out: dict[str, Refined] = {}
    for key, value in _as_dict(_load(path)).items():
        try:
            out[key] = Refined.model_validate(value)
        except ValidationError:
            continue  # an older stored shape: left out, not an error
    return out


def _check(c: CheckResult | None) -> Check | None:
    if c is None:
        return None
    return Check(name=c.name, ok=c.ok, seconds=c.seconds, output=c.output)


def _step(phase: Literal["test", "build", "fix"], a: Attempt) -> Step:
    run = a.run
    return Step(
        phase=phase,
        n=a.n,
        summary=a.summary,
        error=a.error,
        reverted=a.reverted,
        checks=[
            Check(name=c.name, ok=c.ok, seconds=c.seconds, output=c.output)
            for c in a.checks
        ],
        cost_usd=run.cost_usd if run else 0.0,
        seconds=run.latency_ms / 1000 if run else 0.0,
    )


class _Stored(BaseModel):
    """One run as found on disk, before it becomes a summary or a trace."""

    file: str
    key: str
    kind: RunKind
    judged: Judged
    gated: Gated | None = None
    pr_body: str | None = None
    evidence: list[str] = []  # file names in the run's folder
    trace: str = ""  # the run's id in data/traces
    pr_url: str | None = None
    handoff: Handoff | None = None


def _runs_in(path: Path) -> list[_Stored]:
    """The runs in one file; entries in an older stored shape are left out."""
    file, raw = path.stem, _load(path)
    prefix = file.split("-", 1)[0]
    if prefix == "eval" or prefix == "features" or prefix == "wrong":
        return _judged(file, prefix, _as_dict(raw))
    if prefix == "gate":
        try:
            return [
                s for case in _objects.validate_python(raw) if (s := _gate(file, case))
            ]
        except ValidationError:
            return []
    try:
        record = RunRecord.model_validate(raw)
    except ValidationError:
        return []
    judged = Judged(result=record.result, judge=record.judge, valid=record.valid)
    body = path.with_suffix(".pr.md")
    return [
        _Stored(
            file=file,
            key=judged.result.key,
            kind="run",
            judged=judged,
            gated=record.gate,
            pr_body=body.read_text("utf-8") if body.is_file() else None,
            evidence=record.evidence,
            trace=record.trace,
            pr_url=_pr_url(record.published),
            handoff=record.handoff,
        )
    ]


def _pr_url(published: dict[str, object] | None) -> str | None:
    """The PR `run --apply` opened; None for a dry run or a failed publish."""
    url = (published or {}).get("url")
    return (
        url if isinstance(url, str) and url.startswith("https://github.com/") else None
    )


def _judged(file: str, kind: RunKind, entries: dict[str, object]) -> list[_Stored]:
    out: list[_Stored] = []
    for key, value in entries.items():
        try:
            judged = Judged.model_validate(value)
        except ValidationError:
            continue
        out.append(_Stored(file=file, key=key, kind=kind, judged=judged))
    return out


def _gate(file: str, case: dict[str, object]) -> _Stored | None:
    """One `eval gate` case: the gate's final change, judged by the hidden
    repro (and the witness test for wrong fixes)."""
    try:
        gated = Gated.model_validate(case["gated"])
        judge = CheckResult.model_validate(case["judge"]) if case.get("judge") else None
    except (ValidationError, KeyError):
        return None
    key = f"{case.get('key')}-{case.get('kind')}"
    judged = Judged(result=gated.result, judge=judge)
    return _Stored(file=file, key=key, kind="gate", judged=judged, gated=gated)


def _summary(s: _Stored, title: str) -> RunSummary:
    r: LoopResult = s.judged.result
    status = s.gated.status if s.gated is not None else r.status
    cost = r.cost_usd + (s.gated.cost_usd if s.gated is not None else 0.0)
    return RunSummary(
        file=s.file,
        key=s.key,
        kind=s.kind,
        repo=r.repo,
        number=r.number,
        ticket=r.key,
        title=title,
        status=status,
        judge=None if s.judged.judge is None else s.judged.judge.ok,
        valid=None if s.judged.valid is None else s.judged.valid.ok,
        tries=len(r.attempts),
        cost_usd=round(cost, 4),
        made_at=_stamp(s.file),
    )


def _handoff(s: _Stored, clones_dir: Path) -> HandoffView | None:
    h = s.handoff
    if h is None:
        return None
    clone = str(clone_dir(clones_dir, s.judged.result.repo).resolve())
    return HandoffView(
        branch=h.branch,
        commits=h.commits,
        clone=clone,
        fetch=take_command(clone, h.branch) if h.branch else "",
        patch_url=f"/api/patches/{s.file}" if h.patch else "",
        error=h.error,
    )


def _trace(s: _Stored, title: str, clones_dir: Path) -> RunTrace:
    r = s.judged.result
    rounds: list[ReviewRound] = []
    if s.gated is not None:
        for rd in s.gated.rounds:
            rv = rd.review.reviewed
            rounds.append(
                ReviewRound(
                    verdict=rv.verdict,
                    approved=rd.review.approved,
                    summary=rv.summary,
                    test_note=rv.test_note,
                    cause_note=rv.cause_note,
                    findings=[FindingView(**f.model_dump()) for f in rv.findings],
                    proofs=[
                        ProofView(
                            finding=FindingView(**p.finding.model_dump()),
                            proven=p.proven,
                            quote=p.quote,
                            tests=p.tests,
                            red=_check(p.red),
                        )
                        for p in rd.proofs
                    ],
                    fix=[_step("fix", a) for a in rd.attempts],
                )
            )
    final = s.gated.result if s.gated is not None else r
    return RunTrace(
        **_summary(s, title).model_dump(),
        commit=r.commit,
        red=_check(r.red),
        steps=[
            *[_step("test", a) for a in r.reproduce],
            *[_step("build", a) for a in r.attempts],
        ],
        changed=final.changed,
        diff=final.diff,
        gate=None if s.gated is None else s.gated.status,
        rounds=rounds,
        pr_body=s.pr_body,
        sentinel=s.gated.sentinel if s.gated is not None else [],
        evidence=[f"/api/runs/{s.file}/evidence/{n}" for n in s.evidence],
        trace=s.trace,
        pr_url=s.pr_url,
        handoff=_handoff(s, clones_dir),
    )


# ---- the app ----------------------------------------------------------------


def build(
    refine_dir: Path,
    runs_dir: Path,
    clones_dir: Path,
    ui_dir: Path | None,
    port: int | None = None,
    write_token: str | None = None,
    flows: FlowRunner | None = None,
    trace_dir: Path = Path("data/traces"),
    docs_dir: Path = runsteps.DOCS,
    issues_dir: Path = Path("data/issues"),
    targets_dir: Path = Path("targets"),
    tickets_dir: Path = Path("data/tickets"),
    triager: Triager | None = None,
    github: Callable[[str], IssueSource | None] | None = None,
    access: token_mod.Access | None = None,
    setups: repo_jobs.RepoJobs | None = None,
) -> FastAPI:
    """The app over these folders. `ui_dir`: the built UI (`ui/dist`), served
    at `/` when it exists. `port`: the port it serves on; with it, the
    trust boundary (`serve_guard`: Host on every request, Origin + JSON +
    `write_token` on anything that isn't a read) is installed. `triager`:
    the readiness read before a ticket is saved; None: no ticket is written.
    `github`: the read-only source for an import (None: no token); `access`:
    what the token can do, checked once at start. `setups`: the dev's own
    repos being added (read here; proposed and checked by a write server)."""
    app = FastAPI(
        title="cofabriq",
        version=importlib.metadata.version("cofabriq"),
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        # operation ids = the route function names: the UI's generated hooks
        # read `useSpecs`, `useRun`, not `specsApiSpecsGet`
        generate_unique_id_function=lambda route: route.name,
        # no span per request: the page polls, and each poll would be a trace
        telemetry={"tracing": False},
    )

    @cache
    def files_at(repo: str, commit: str) -> frozenset[str] | None:
        clone = clone_dir(clones_dir, repo)
        if not (clone / ".git").exists():
            return None
        try:
            return frozenset(git_mod.files_at(clone, commit))
        except subprocess.CalledProcessError:  # the commit isn't in the clone
            return None

    def readiness(r: Refined) -> list[str] | None:
        files = files_at(r.repo, r.commit)
        return None if files is None else not_ready(r, set(files))

    @app.get("/api/specs", response_model=list[SpecSummary])
    def specs() -> list[SpecSummary]:
        out: list[SpecSummary] = []
        for path in _files(refine_dir):
            for key, r in _specs_in(path).items():
                out.append(
                    SpecSummary(
                        file=path.stem,
                        key=key,
                        repo=r.repo,
                        number=r.number,
                        ticket=ticket_key(r.source, r.number),
                        route=r.route,
                        size=r.spec.size,
                        files=len(r.spec.files),
                        criteria=len(r.spec.criteria),
                        open_questions=len(r.spec.open_questions),
                        not_ready=readiness(r),
                        made_at=_stamp(path.stem),
                    )
                )
        return out

    @app.get("/api/specs/{file}/{key}", response_model=SpecView)
    def spec(file: str, key: str) -> SpecView:
        found = _specs_in(_file(refine_dir, file)).get(key)
        if found is None:
            raise HTTPException(status_code=404, detail="no such spec")
        s = found.spec
        return SpecView(
            file=file,
            key=key,
            repo=found.repo,
            number=found.number,
            ticket=ticket_key(found.source, found.number),
            commit=found.commit,
            route=found.route,
            size=s.size,
            problem=s.problem,
            files=[_note(f) for f in s.files],
            tests=[_note(f) for f in s.tests],
            proof=_proof(s),
            criteria=s.criteria,
            plan=s.plan,
            assumptions=s.assumptions,
            open_questions=s.open_questions,
            unknown_paths=found.unknown_paths,
            not_ready=readiness(found),
            cost_usd=found.run.cost_usd,
            turns=found.run.turns,
            seconds=found.run.latency_ms / 1000,
        )

    def title(s: _Stored) -> str:
        """A run on an issue: the issue's title, if the store has it."""
        r = s.judged.result
        if s.kind != "run":
            return ""
        found = tickets_mod.find(issues_dir, tickets_dir, r.repo, r.key)
        return found.title if found is not None else ""

    @app.get("/api/runs", response_model=list[RunSummary])
    def runs() -> list[RunSummary]:
        return [
            _summary(s, title(s)) for path in _files(runs_dir) for s in _runs_in(path)
        ]

    @app.get("/api/runs/{file}/evidence/{name}", include_in_schema=False)
    def evidence(file: str, name: str) -> FileResponse:
        """A screenshot of a run, only from that run's own folder."""
        _file(runs_dir, file)  # the run must exist
        folder = runs_dir / file
        if not _NAME.match(name) or not (folder / name).is_file():
            raise HTTPException(status_code=404, detail="no such file")
        if (folder / name).resolve().parent != folder.resolve():
            raise HTTPException(status_code=404, detail="no such file")
        return FileResponse(folder / name)

    @app.get("/api/runs/{file}/{key}", response_model=RunTrace)
    def run(file: str, key: str) -> RunTrace:
        for s in _runs_in(_file(runs_dir, file)):
            if s.key == key:
                return _trace(s, title(s), clones_dir)
        raise HTTPException(status_code=404, detail="no such run")

    @app.get("/api/patches/{file}", include_in_schema=False)
    def patch(file: str) -> FileResponse:
        """A run's change as a patch to download, only the one its record
        names. Agent-written text: sent as an attachment, never shown."""
        names = [s.handoff.patch for s in _runs_in(_file(runs_dir, file)) if s.handoff]
        name = names[0] if names else ""
        if not _NAME.match(name) or not (runs_dir / name).is_file():
            raise HTTPException(status_code=404, detail="no patch for this run")
        return FileResponse(
            runs_dir / name,
            media_type="text/x-diff",
            filename=name,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/traces/{run}", response_model=StepTree)
    def trace(run: str) -> StepTree:
        """A run's steps as a tree, the running one included (poll it)."""
        found = runsteps.load_steps(trace_dir, run, runsteps.load_docs(docs_dir))
        if found is None:
            raise HTTPException(status_code=404, detail="no such trace")
        return StepTree(steps=found, totals=runsteps.totals(found))

    @app.get("/api/traces/{run}/payloads/{span}", response_model=dict[str, JsonValue])
    def payload(run: str, span: str) -> dict[str, JsonValue]:
        """What one step got and gave: a prompt and its answer, a command's
        output, the state a node changed. Untrusted text: show it as text."""
        found = runsteps.load_payload(trace_dir, run, span)
        if found is None:
            raise HTTPException(status_code=404, detail="nothing kept for this step")
        return _json.validate_python(found)

    @app.get("/api/step-docs", response_model=dict[str, runsteps.StepDoc])
    def step_docs() -> dict[str, runsteps.StepDoc]:
        """Every step's title, sentence and why (docs/steps/), keyed by `doc`."""
        return runsteps.load_docs(docs_dir)

    @app.get("/api/code/{owner}/{name}", response_model=CodeLines)
    def code(
        owner: str,
        name: str,
        commit: str,
        path: str,
        line: int | None = None,
        end: int | None = None,
    ) -> CodeLines:
        """A file at a commit: lines `line` to `end` (default: `line`) with 8
        above and below, at most 80; the top of the file without `line`. Only
        a file in that commit's tree of the local clone: never a path from
        outside it."""
        repo = f"{owner}/{name}"
        if not _REPO.match(repo) or not _COMMIT.match(commit):
            raise HTTPException(status_code=404, detail="no such file")
        files = files_at(repo, commit)
        if files is None or path not in files:
            raise HTTPException(status_code=404, detail="no such file at this commit")
        try:
            text = git_mod.git(clone_dir(clones_dir, repo), "show", f"{commit}:{path}")
        except (subprocess.CalledProcessError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=404, detail="not a text file") from exc
        lines = text.splitlines()
        if line is None or not 1 <= line <= len(lines):
            first, last = 1, 2 * _AROUND + 1
        else:
            first, last = line - _AROUND, max(line, end or line) + _AROUND
        first = max(1, first)
        last = min(len(lines), last, first + _MAX_LINES - 1)
        return CodeLines(
            path=path, start=first, lines=lines[first - 1 : last], total=len(lines)
        )

    def runnable(repo: str) -> bool:
        """`[loop]` in the target file, proven by `repo check` if the
        dev's own."""
        return (
            load_rules(targets_dir, repo) is not None
            and unproven(targets_dir, repo) is None
        )

    def runnable_repo(owner: str, name: str) -> str:
        repo = f"{owner}/{name}"
        if not _REPO.match(repo) or not runnable(repo):
            raise HTTPException(status_code=404, detail="no such repo")
        return repo

    @app.get("/api/repos", response_model=list[Repo])
    def repos() -> list[Repo]:
        """The repos a flow can run on: `[loop]` in the target file (proven
        if the dev's own), and an issue store (`fetch-issues`) or local
        tickets; a proven own repo also with neither (just added)."""
        out: list[Repo] = []
        found = {
            f"{folder.parent.name}/{folder.name}"
            for root in (issues_dir, tickets_dir)
            for folder in root.glob("*/*")
            if folder.is_dir()
        } | set(setups.repos() if setups is not None else [])
        for repo in sorted(found):
            if _REPO.match(repo) and runnable(repo):
                owner, name = repo.split("/")
                issues = len(load_index(issues_dir / owner / name))
                tickets = len(tickets_mod.load_tickets(tickets_dir, repo))
                github = on_github(targets_dir, repo)
                out.append(
                    Repo(repo=repo, issues=issues, tickets=tickets, on_github=github)
                )
        return out

    @app.get("/api/github", response_model=GitHubAccess)
    def github_token() -> GitHubAccess:
        """The token's source and whether it could write, as checked when
        the server started."""
        a = access or token_mod.access(None, None)
        return GitHubAccess(source=a.source, can_write=a.can_write, warning=a.warning)

    def flows_by_ticket(repo: str) -> dict[str, FlowSummary]:
        out: dict[str, FlowSummary] = {}
        for t in flows.threads() if flows is not None else []:
            if t.startswith(f"{repo}#") and flows is not None:
                status = flows.status(t)
                if status is not None:
                    out[status.state.issue.key] = _flow_summary(status)
        return out

    @app.get("/api/issues/{owner}/{name}", response_model=list[IssueRow])
    def issues(owner: str, name: str) -> list[IssueRow]:
        """A repo's local tickets, then its stored issues, newest first. Read
        from data/, not from GitHub: Start refreshes the one it starts."""
        repo = runnable_repo(owner, name)
        by_ticket = flows_by_ticket(repo)
        found = [
            *reversed(tickets_mod.load_tickets(tickets_dir, repo)),
            *sorted(
                load_issues(issues_dir, repo), key=lambda i: i.number, reverse=True
            ),
        ]
        return [_issue_row(i, by_ticket.get(i.key)) for i in found]

    @app.get("/api/issues/{owner}/{name}/{ticket}", response_model=IssueView)
    def issue(owner: str, name: str, ticket: TicketKey) -> IssueView:
        """One stored issue or local ticket with its text, as last fetched."""
        repo = runnable_repo(owner, name)
        found = tickets_mod.find(issues_dir, tickets_dir, repo, ticket)
        if found is None:
            raise HTTPException(status_code=404, detail="no such issue")
        thread = f"{check_repo(repo)}#{ticket}"
        status = flows.status(thread) if flows is not None else None
        flow = _flow_summary(status) if status is not None else None
        return IssueView(**_issue_row(found, flow).model_dump(), body=found.body)

    if flows is not None:  # the write server: flows and tickets
        _flow_routes(app, flows)

        @app.post("/api/tickets/{owner}/{name}", response_model=TicketSaved)
        def ticket_new(owner: str, name: str, new: NewTicket) -> TicketSaved:
            """Write a local ticket: one LLM call reads it first and says what
            is missing (it fills nothing in); with gaps it is saved only with
            `anyway`. Never sent to GitHub. Paid (about $0.001)."""
            repo = runnable_repo(owner, name)
            if triager is None:
                raise HTTPException(status_code=503, detail="no OPENAI_API_KEY")
            components = triage_mod.load_components(targets_dir, repo)
            try:
                found = tickets_mod.check(
                    repo, new.title, new.body, components, triager
                )
                saved = None
                if not found.missing or new.anyway:
                    t = tickets_mod.new_ticket(tickets_dir, repo, new.title, new.body)
                    saved = _issue_row(t, None)
            except ValueError as exc:  # a blank title once stripped
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return TicketSaved(
                type=found.type,
                missing=found.missing,
                cost_usd=found.cost_usd,
                saved=saved,
            )

        @app.post(
            "/api/repos/{owner}/{name}/import",
            response_model=Imported,
            responses={409: {"model": Refusal}},
        )
        def import_issues(owner: str, name: str, ask: ImportAsk) -> Imported:
            """The newest open issues from GitHub into the store (read-only
            towards GitHub). More open than `limit`: nothing is stored and
            `asked` says so, until `confirm`. One GraphQL request."""
            repo = runnable_repo(owner, name)
            if not on_github(targets_dir, repo):
                detail = f"{repo} is not on github.com: write local tickets"
                raise HTTPException(status_code=409, detail=detail)
            source = github(repo) if github is not None else None
            if source is None:
                raise HTTPException(status_code=503, detail="no GitHub token")
            try:
                issues, total = source.open_issues(ask.limit)
            except (GitHubError, httpx.HTTPError) as exc:
                raise HTTPException(status_code=502, detail=f"GitHub: {exc}") from exc
            if total > ask.limit and not ask.confirm:
                return Imported(total=total, written=0, unchanged=0, asked=True)
            got = store_open(issues_dir, repo, issues, total)
            return Imported(
                total=total, written=got.written, unchanged=got.unchanged, asked=False
            )

    if setups is not None:
        _setup_routes(app, setups, clones_dir, write=flows is not None)

    if port is not None:
        guard(app, port, write_token, dev_origins=("http://127.0.0.1:5173",))

    if ui_dir is not None and (ui_dir / "index.html").is_file():
        index = ui_dir / "index.html"
        app.mount("/assets", StaticFiles(directory=ui_dir / "assets"), name="assets")

        @app.get("/{rest:path}", include_in_schema=False)
        def ui(rest: str) -> FileResponse:
            """A file of the build (favicon), else the app: its client-side
            routes (/specs/...) all load index.html."""
            root = ui_dir.resolve()
            asked = (root / rest).resolve()
            if rest and asked.is_relative_to(root) and asked.is_file():
                return FileResponse(asked)
            return FileResponse(index)

    return app


def _flow_summary(f: FlowStatus) -> FlowSummary:
    st = f.state
    outcome = f"{st.outcome}: {st.note}" if st.note else st.outcome
    if f.cut_off:  # the checkpoint's stage is where it stopped, not how it ended
        outcome = f"cut off: the server stopped during {st.stage}"
    return FlowSummary(
        thread=f.thread,
        repo=st.issue.repo,
        number=st.issue.number,
        ticket=st.issue.key,
        url=_github_url(st.issue),
        title=st.issue.title,
        route=st.route,
        waiting=f.waiting,
        running=f.running,
        phase=_phase(f),
        outcome="waiting" if f.waiting and not f.running else outcome,
        error=f.error,
    )


def _phase(f: FlowStatus) -> Phase:
    """Where a flow is: the gate it waits at, else the part after the last
    step its checkpoint finished (that part runs now, or the flow ended
    there). The stage alone can't say it: "rejected" is either gate,
    "failed" is refine, setup or the test."""
    st = f.state
    if f.waiting is not None:
        return "plan" if f.waiting == "plan" else "test_gate"
    if st.refined is None or (st.stage == "failed" and st.progress is None):
        return "spec"
    match st.stage:
        case "refined":  # refine done, the plan gate not reached: cut off
            return "spec"
        case "rejected":
            return "plan" if st.progress is None else "test_gate"
        case "approved" | "set_up" | "retest" | "failed":
            return "test"
        case "tested":  # the test gate approved it
            return "build"
        case "built":
            green = st.result is not None and st.result.status == "green"
            return "review" if green else "build"
        case "reviewed":  # no gate: the review was halted
            blocked = st.gated is None or st.gated.status == "blocked"
            return "review" if blocked else "done"


def _github_url(i: Issue) -> str | None:
    if i.source == "local":
        return None
    return f"https://github.com/{i.repo}/issues/{i.number}"


def _issue_row(i: Issue, flow: FlowSummary | None) -> IssueRow:
    return IssueRow(
        number=i.number,
        ticket=i.key,
        source=i.source,
        title=i.title,
        state=i.state,
        labels=i.labels,
        comments=i.comment_count,
        updated_at=i.updated_at,
        url=_github_url(i),
        flow=flow,
    )


def _flow_routes(app: FastAPI, flows: FlowRunner) -> None:
    """The browser gate: list flows, show one, start one, answer its gate.
    Start and answer change something; `serve_guard` decides who may call
    them."""

    def thread(owner: str, name: str, ticket: str) -> str:
        try:
            return f"{check_repo(f'{owner}/{name}')}#{ticket}"
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="no such flow") from exc

    @app.get("/api/flows", response_model=list[FlowSummary])
    def flow_list() -> list[FlowSummary]:
        found = [flows.status(t) for t in flows.threads()]
        return [_flow_summary(f) for f in found if f is not None]

    @app.get("/api/flows/{owner}/{name}/{ticket}", response_model=FlowView)
    def flow(owner: str, name: str, ticket: TicketKey) -> FlowView:
        f = flows.status(thread(owner, name, ticket))
        if f is None:
            raise HTTPException(status_code=404, detail="no such flow")
        spec = f.state.refined.spec if f.state.refined else None
        return FlowView(
            **_flow_summary(f).model_dump(),
            body=f.state.issue.body,
            size=spec.size if spec else None,
            problem=spec.problem if spec else "",
            criteria=spec.criteria if spec else [],
            files=[_note(x) for x in spec.files] if spec else [],
            plan=spec.plan if spec else [],
            assumptions=spec.assumptions if spec else [],
            open_questions=spec.open_questions if spec else [],
            diff=f.diff,
            red=f.red,
            retests_left=f.retests_left,
            commit=f.state.refined.commit if f.state.refined else "",
            tests=[_note(x) for x in spec.tests] if spec else [],
            proof=_proof(spec),
            unknown_paths=f.state.refined.unknown_paths if f.state.refined else [],
            trace=tracing.run_id(f.state.trace),
            record=f.state.record,
        )

    @app.post("/api/flows/{owner}/{name}/{ticket}/answer", response_model=Answered)
    def flow_answer(
        owner: str, name: str, ticket: TicketKey, answer: Answer
    ) -> Answered:
        if not flows.answer(thread(owner, name, ticket), answer):
            raise HTTPException(status_code=409, detail="not waiting at a gate")
        return Answered(started=True)

    @app.post(
        "/api/flows/{owner}/{name}/{ticket}/start",
        response_model=Started,
        responses={409: {"model": Refusal}},
    )
    def flow_start(owner: str, name: str, ticket: TicketKey, start: Start) -> Started:
        """Start a flow on a stored issue or local ticket: the issue
        refreshed from GitHub (a local ticket as stored), its blockers
        checked, the clone updated (here, so a refusal comes back at once),
        then refine in the background up to the plan gate. Paid: an agent
        run."""
        repo = thread(owner, name, ticket).split("#", 1)[0]
        try:
            return Started(
                thread=flows.start(repo, ticket, start.route, again=start.again)
            )
        except Refused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def _setup_routes(
    app: FastAPI, setups: repo_jobs.RepoJobs, clones_dir: Path, write: bool
) -> None:
    """Adding the dev's own repo: what each one's setup is (read), and on
    the write server proposing its `[loop]` and checking it. The two writes
    start background work the page polls; `serve_guard` decides who may."""

    def view(s: repo_jobs.RepoSetup) -> RepoSetup:
        clone = str(clone_dir(clones_dir, s.repo).resolve())
        return RepoSetup(**s.model_dump(), clone=clone)

    def setup_of(owner: str, name: str) -> str:
        repo = f"{owner}/{name}"
        if not _REPO.match(repo):
            raise HTTPException(status_code=404, detail="no such repo")
        return check_repo(repo)

    @app.get("/api/repos/own", response_model=list[RepoSetup])
    def repo_setups() -> list[RepoSetup]:
        """The dev's own repos (`targets/own/`), each as far as its setup got."""
        found = [setups.status(r) for r in setups.repos()]
        return [view(s) for s in found if s is not None]

    @app.get("/api/repos/{owner}/{name}/setup", response_model=RepoSetup)
    def repo_setup(owner: str, name: str) -> RepoSetup:
        found = setups.status(setup_of(owner, name))
        if found is None:
            raise HTTPException(status_code=404, detail="not added")
        return view(found)

    if not write:
        return

    @app.post(
        "/api/repos/add",
        response_model=SetupStarted,
        responses={409: {"model": Refusal}},
    )
    def repo_add(add: AddRepo) -> SetupStarted:
        """Propose `[loop]` for the dev's clone at `path`: cofabriq clones it
        into its own copy (the dev's folder is only read), and a read-only
        agent reads how it builds there, in the background. Nothing runs
        until the check. Paid: one agent run (about $0.10)."""
        try:
            repo = setups.propose(Path(add.path), again=add.again, repair=add.repair)
            return SetupStarted(repo=repo)
        except repo_jobs.Refused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/repos/{owner}/{name}/check",
        response_model=SetupStarted,
        responses={409: {"model": Refusal}},
    )
    def repo_check(owner: str, name: str, check: CheckRepo) -> SetupStarted:
        """Save the target file as the dev edited it (refused when it doesn't
        parse), then prove it in the sandbox in the background: install
        (the only network), every check, each suite's canaries. Free,
        minutes."""
        repo = setup_of(owner, name)
        try:
            setups.check(repo, check.config)
        except repo_jobs.Refused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return SetupStarted(repo=repo)
