"""`cofabriq serve`: an HTTP API over what refine and the loop stored, plus
the built UI.

    data/refine/*.json   specs: {key: Refined}
    data/runs/*.json     runs:  {key: Judged} (eval-, features-, wrong-),
                                [GateCase] (gate-), or one run record
                                (<owner>-<name>-<n>-<stamp>.json, `run`)
    data/traces/         a flow's steps while and after it runs
                                (`runsteps.py`), what each step got and gave

The API returns its own response models, not the stored ones: a stored
shape can change without changing the UI's contract (ADR-009). Everything
is read per request from the files, nothing is cached. It binds to
127.0.0.1 only. Every route reads, except one: with `--write`, the browser
gate answers a paused flow's gate (`_flow_routes`), behind the token and
the checks in `serve_guard`. File names in URLs must be files in the
folder (no paths, no `..`).
"""

import importlib.metadata
import json
import re
import subprocess
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

from cofabriq import git as git_mod
from cofabriq import runsteps, tracing
from cofabriq.eval.loop import Judged
from cofabriq.files import STAMP
from cofabriq.issues.fetch import load_index, load_issues
from cofabriq.issues.model import check_repo
from cofabriq.loop.gate import Gated
from cofabriq.loop.graph import FlowRunner, FlowStatus, GateAnswer, Refused
from cofabriq.loop.intake import not_ready
from cofabriq.loop.record import RunRecord
from cofabriq.loop.refine import Refined, Route
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


# ---- the contract (what the UI sees) -----------------------------------------


class SpecSummary(BaseModel):
    file: str
    key: str
    repo: str
    number: int
    route: Literal["bug", "feature"]
    size: str
    files: int
    criteria: int
    open_questions: int
    not_ready: list[str] | None  # None: the clone isn't there to check paths
    made_at: datetime | None


class FileNote(BaseModel):
    path: str
    why: str
    at: list[str] = Field(description='The lines it changes: "74", "78-83".')


class SpecView(BaseModel):
    file: str
    key: str
    repo: str
    number: int
    commit: str
    route: Literal["bug", "feature"]
    size: str
    problem: str
    files: list[FileNote]
    tests: list[FileNote]
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
    status: str  # the loop's status, or the gate's when it ran
    judge: bool | None  # hidden repro / reference tests green
    valid: bool | None  # the agent's test right on the reference
    tries: int
    cost_usd: float
    made_at: datetime | None


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


class FlowSummary(BaseModel):
    """A run in the browser gate: where it waits, or how it ended."""

    thread: str
    repo: str
    number: int
    title: str
    route: Literal["bug", "feature"]
    waiting: Literal["plan", "test"] | None
    running: bool
    # e.g. "approved", "escalated", "rejected: plan rejected", "cut off: ..."
    outcome: str
    error: str


class FlowView(FlowSummary):
    size: str | None
    problem: str
    criteria: list[str]
    files: list[FileNote]
    plan: list[str]
    assumptions: list[str]
    open_questions: list[str]
    diff: str  # the frozen test, at the test gate
    commit: str  # the spec's: its paths and code lines are at it; "" before refine
    tests: list[FileNote]
    unknown_paths: list[str]  # not in the repo at the commit: new files
    trace: str  # its steps so far: /api/traces/<trace>; "" untraced
    record: str  # the finished run: /api/runs/<record>/<number>; "" before


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
    """A repo a flow can run on: an issue store and `[loop]` in its target
    file."""

    repo: str
    issues: int


class IssueRow(BaseModel):
    """One stored issue, as the Issues page lists it."""

    number: int
    title: str
    state: str = Field(description="`open` or `closed`, as last fetched.")
    labels: list[str]
    comments: int
    updated_at: datetime
    url: str = Field(description="The issue on GitHub.")
    flow: FlowSummary | None = Field(
        description="Its flow, where it waits or how it ended; null: none yet."
    )


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


class Answer(GateAnswer):
    # the gate's answer under the API's name; a longer reason is a 422 here
    reason: str = Field(default="", max_length=500)


class Answered(BaseModel):
    started: bool


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
            key=str(judged.result.number),
            kind="run",
            judged=judged,
            gated=record.gate,
            pr_body=body.read_text("utf-8") if body.is_file() else None,
            evidence=record.evidence,
            trace=record.trace,
            pr_url=_pr_url(record.published),
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


def _summary(s: _Stored) -> RunSummary:
    r: LoopResult = s.judged.result
    status = s.gated.status if s.gated is not None else r.status
    cost = r.cost_usd + (s.gated.cost_usd if s.gated is not None else 0.0)
    return RunSummary(
        file=s.file,
        key=s.key,
        kind=s.kind,
        repo=r.repo,
        number=r.number,
        status=status,
        judge=None if s.judged.judge is None else s.judged.judge.ok,
        valid=None if s.judged.valid is None else s.judged.valid.ok,
        tries=len(r.attempts),
        cost_usd=round(cost, 4),
        made_at=_stamp(s.file),
    )


def _trace(s: _Stored) -> RunTrace:
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
        **_summary(s).model_dump(),
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
) -> FastAPI:
    """The app over these folders. `ui_dir`: the built UI (`ui/dist`), served
    at `/` when it exists. `port`: the port it serves on; with it, the
    trust boundary (`serve_guard`: Host on every request, Origin + JSON +
    `write_token` on anything that isn't a read) is installed."""
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
            commit=found.commit,
            route=found.route,
            size=s.size,
            problem=s.problem,
            files=[FileNote(path=f.path, why=f.why, at=f.at) for f in s.files],
            tests=[FileNote(path=f.path, why=f.why, at=f.at) for f in s.tests],
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

    @app.get("/api/runs", response_model=list[RunSummary])
    def runs() -> list[RunSummary]:
        return [_summary(s) for path in _files(runs_dir) for s in _runs_in(path)]

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
                return _trace(s)
        raise HTTPException(status_code=404, detail="no such run")

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
        return load_rules(targets_dir, repo) is not None

    @app.get("/api/repos", response_model=list[Repo])
    def repos() -> list[Repo]:
        """The repos a flow can run on: an issue store (`fetch-issues`) and
        `[loop]` in the target file."""
        out: list[Repo] = []
        for folder in sorted(issues_dir.glob("*/*")):
            repo = f"{folder.parent.name}/{folder.name}"
            if folder.is_dir() and _REPO.match(repo) and runnable(repo):
                out.append(Repo(repo=repo, issues=len(load_index(folder))))
        return out

    @app.get("/api/issues/{owner}/{name}", response_model=list[IssueRow])
    def issues(owner: str, name: str) -> list[IssueRow]:
        """A repo's stored issues, newest first. Read from data/issues, not
        from GitHub: Start refreshes the one it starts."""
        repo = f"{owner}/{name}"
        if not _REPO.match(repo) or not runnable(repo):
            raise HTTPException(status_code=404, detail="no such repo")
        by_number: dict[int, FlowSummary] = {}
        for t in flows.threads() if flows is not None else []:
            if t.startswith(f"{repo}#") and flows is not None:
                status = flows.status(t)
                if status is not None:
                    by_number[status.state.issue.number] = _flow_summary(status)
        found = sorted(
            load_issues(issues_dir, repo), key=lambda i: i.number, reverse=True
        )
        return [
            IssueRow(
                number=i.number,
                title=i.title,
                state=i.state,
                labels=i.labels,
                comments=i.comment_count,
                updated_at=i.updated_at,
                url=f"https://github.com/{repo}/issues/{i.number}",
                flow=by_number.get(i.number),
            )
            for i in found
        ]

    if flows is not None:
        _flow_routes(app, flows)

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
        title=st.issue.title,
        route=st.route,
        waiting=f.waiting,
        running=f.running,
        outcome="waiting" if f.waiting and not f.running else outcome,
        error=f.error,
    )


def _flow_routes(app: FastAPI, flows: FlowRunner) -> None:
    """The browser gate: list flows, show one, answer its gate. The answer
    is the only route that changes anything; `serve_guard` decides who may
    call it."""

    def thread(owner: str, name: str, number: int) -> str:
        try:
            return f"{check_repo(f'{owner}/{name}')}#{number}"
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="no such flow") from exc

    @app.get("/api/flows", response_model=list[FlowSummary])
    def flow_list() -> list[FlowSummary]:
        found = [flows.status(t) for t in flows.threads()]
        return [_flow_summary(f) for f in found if f is not None]

    @app.get("/api/flows/{owner}/{name}/{number}", response_model=FlowView)
    def flow(owner: str, name: str, number: int) -> FlowView:
        f = flows.status(thread(owner, name, number))
        if f is None:
            raise HTTPException(status_code=404, detail="no such flow")
        spec = f.state.refined.spec if f.state.refined else None
        return FlowView(
            **_flow_summary(f).model_dump(),
            size=spec.size if spec else None,
            problem=spec.problem if spec else "",
            criteria=spec.criteria if spec else [],
            files=[FileNote(path=x.path, why=x.why, at=x.at) for x in spec.files]
            if spec
            else [],
            plan=spec.plan if spec else [],
            assumptions=spec.assumptions if spec else [],
            open_questions=spec.open_questions if spec else [],
            diff=f.diff,
            commit=f.state.refined.commit if f.state.refined else "",
            tests=[FileNote(path=x.path, why=x.why, at=x.at) for x in spec.tests]
            if spec
            else [],
            unknown_paths=f.state.refined.unknown_paths if f.state.refined else [],
            trace=tracing.run_id(f.state.trace),
            record=f.state.record,
        )

    @app.post("/api/flows/{owner}/{name}/{number}/answer", response_model=Answered)
    def flow_answer(owner: str, name: str, number: int, answer: Answer) -> Answered:
        if not flows.answer(thread(owner, name, number), answer):
            raise HTTPException(status_code=409, detail="not waiting at a gate")
        return Answered(started=True)

    @app.post(
        "/api/flows/{owner}/{name}/{number}/start",
        response_model=Started,
        responses={409: {"model": Refusal}},
    )
    def flow_start(owner: str, name: str, number: int, start: Start) -> Started:
        """Start a flow on a stored issue: the issue refreshed from GitHub,
        its blockers checked, the clone updated (here, so a refusal comes
        back at once), then refine in the background up to the plan gate.
        Paid: an agent run."""
        repo = thread(owner, name, number).split("#", 1)[0]
        try:
            return Started(
                thread=flows.start(repo, number, start.route, again=start.again)
            )
        except Refused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
