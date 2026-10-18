"""`cofabriq serve`: a read-only HTTP API over what refine and the loop
stored, plus the built UI (0.3).

    data/refine/*.json   specs: {key: Refined}
    data/runs/*.json     runs:  {key: Judged} (eval-, features-, wrong-),
                                [GateCase] (gate-), or one run record
                                (<owner>-<name>-<n>-<stamp>.json, `run`)

The API returns its own response models, not the stored ones: a stored
shape can change without changing the UI's contract (ADR-009). Everything
is read per request from the files, nothing is cached or written. Read-only
by design: no route changes anything, so it binds to 127.0.0.1 and needs no
token yet (the browser gate in 0.5 adds one). File names in URLs must be
files in the folder (no paths, no `..`).
"""

import json
import re
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, TypeAdapter, ValidationError

from cofabriq.eval.loop import Judged
from cofabriq.issues.model import check_repo
from cofabriq.loop.gate import Gated
from cofabriq.loop.graph import FlowRunner, FlowStatus
from cofabriq.loop.intake import not_ready
from cofabriq.loop.refine import Refined, tracked
from cofabriq.loop.run import Attempt, CheckResult, LoopResult
from cofabriq.serve_guard import guard

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
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


class FlowSummary(BaseModel):
    """A run in the browser gate: where it waits, or how it ended."""

    thread: str
    repo: str
    number: int
    title: str
    route: Literal["bug", "feature"]
    waiting: Literal["plan", "test"] | None
    running: bool
    outcome: str  # e.g. "approved", "escalated", "rejected: plan rejected"
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


class Answer(BaseModel):
    approve: bool


class Answered(BaseModel):
    started: bool


# ---- reading the files ------------------------------------------------------


def _stamp(file: str) -> datetime | None:
    m = _STAMP.search(file)
    return (
        datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        if m
        else None
    )


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
    record = _as_dict(raw)
    if "result" not in record:
        return []
    try:
        judged = Judged.model_validate(
            {k: record[k] for k in ("result", "judge", "valid") if k in record}
        )
        gated = Gated.model_validate(record["gate"]) if record.get("gate") else None
    except ValidationError:
        return []
    body = path.with_suffix(".pr.md")
    return [
        _Stored(
            file=file,
            key=str(judged.result.number),
            kind="run",
            judged=judged,
            gated=gated,
            pr_body=body.read_text("utf-8") if body.is_file() else None,
        )
    ]


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
) -> FastAPI:
    """The app over these folders. `ui_dir`: the built UI (`ui/dist`), served
    at `/` when it exists. `port`: the port it serves on; with it, the
    trust boundary (`serve_guard`: Host on every request, Origin + JSON +
    `write_token` on anything that isn't a read) is installed."""
    app = FastAPI(
        title="cofabriq",
        version="0.3",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        # operation ids = the route function names: the UI's generated hooks
        # read `useSpecs`, `useRun`, not `specsApiSpecsGet`
        generate_unique_id_function=lambda route: route.name,
    )

    @cache
    def files_at(repo: str, commit: str) -> frozenset[str] | None:
        clone = clones_dir / repo.replace("/", "-")
        if not (clone / ".git").exists():
            return None
        try:
            return frozenset(tracked(clone, commit))
        except Exception:  # the commit isn't in the clone (any more)
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
            files=[FileNote(path=f.path, why=f.why) for f in s.files],
            tests=[FileNote(path=f.path, why=f.why) for f in s.tests],
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

    @app.get("/api/runs/{file}/{key}", response_model=RunTrace)
    def run(file: str, key: str) -> RunTrace:
        for s in _runs_in(_file(runs_dir, file)):
            if s.key == key:
                return _trace(s)
        raise HTTPException(status_code=404, detail="no such run")

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
    outcome = (
        st.gated.status if st.gated else st.result.status if st.result else st.stage
    )
    if st.note:
        outcome = f"{outcome}: {st.note}"
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
            files=[FileNote(path=x.path, why=x.why) for x in spec.files]
            if spec
            else [],
            plan=spec.plan if spec else [],
            assumptions=spec.assumptions if spec else [],
            open_questions=spec.open_questions if spec else [],
            diff=f.diff,
        )

    @app.post("/api/flows/{owner}/{name}/{number}/answer", response_model=Answered)
    def flow_answer(owner: str, name: str, number: int, answer: Answer) -> Answered:
        started = flows.answer(thread(owner, name, number), answer.approve)
        if not started:
            raise HTTPException(status_code=409, detail="not waiting at a gate")
        return Answered(started=True)
