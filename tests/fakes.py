"""Test doubles: agents with scripted answers or edits, a host sandbox, an
embedder, the triage LLM calls, recorded GitHub pages, and the loop and the
flow wired to them."""

import json
import re
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cofabriq.context.ingest.embed import DIM
from cofabriq.issues.github import GitHubIssueSource
from cofabriq.issues.model import Issue
from cofabriq.issues.triage import Fields
from cofabriq.issues.verdict import Answer, Candidate
from cofabriq.llm import Usage
from cofabriq.loop.engine import AgentRun
from cofabriq.loop.graph import Deps
from cofabriq.loop.refine import Spec
from cofabriq.loop.review import Reviewed
from cofabriq.loop.run import Implemented, LoopResult, Reproduced, run_loop
from cofabriq.loop.sandbox import Ran, run_command
from tests.builders import AGENT_RULES, RULES, reviewed, setup, spec


class FakeEmbedder:
    """Bag of words: one dimension per word. Records every text it embeds."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        self.seen.extend(texts)
        return [self.vector(t) for t in texts]

    @staticmethod
    def vector(text: str) -> list[float]:
        v = [0.0] * DIM
        for word in re.findall(r"[a-z]+", text.lower()):
            v[zlib.crc32(word.encode()) % DIM] += 1.0
        return v


class FakeTriager:
    def __init__(self, result: Fields) -> None:
        self.result = result

    def fields(self, issue: Issue, components: dict[str, str]) -> tuple[Fields, Usage]:
        return self.result, Usage(1000, 100, 900)


class FakeJudge:
    def __init__(self, answer: Answer) -> None:
        self.answer = answer

    def judge(
        self, query: Issue, candidates: Sequence[Candidate]
    ) -> tuple[Answer, Usage]:
        return self.answer, Usage(2000, 200, 1200)


# `fixtures/issues_page{1,2}.json`: two recorded pages of open-webui's issues
# (how they were recorded and edited: `test_issues.py`).
FIXTURES = Path(__file__).parent / "fixtures"
RECORDED = "open-webui/open-webui"
RECORDED_AT = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)

type Handler = Callable[[httpx.Request], httpx.Response]


def page(n: int) -> dict[str, Any]:
    return json.loads((FIXTURES / f"issues_page{n}.json").read_text())


def replay(*responses: httpx.Response) -> tuple[Handler, list[dict[str, Any]]]:
    """A handler answering with `responses` in order; records request bodies."""
    queue = list(responses)
    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return queue.pop(0)

    return handle, sent


def source(handler: Handler, sleeps: list[float]) -> GitHubIssueSource:
    return GitHubIssueSource(
        RECORDED,
        "token",
        comments=2,
        page_size=2,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        now=lambda: RECORDED_AT,
    )


def one(node: dict[str, Any] | None) -> httpx.Response:
    """A recorded page node as the answer to `ISSUE_QUERY`."""
    data = {"rateLimit": page(1)["data"]["rateLimit"], "repository": {"issue": node}}
    return httpx.Response(200, json={"data": data})


class HostSandbox:
    """Runs the shell line in the worktree on the host: tests only, where
    every command is the test's own."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, cmd: str, worktree: Path, *, network: bool) -> Ran:
        self.calls.append((cmd, network))
        return run_command(["sh", "-c", cmd], worktree, 30)


type Edit = Callable[[Path], None]


def write(path: str, text: str) -> Edit:
    def edit(cwd: Path) -> None:
        (cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (cwd / path).write_text(text)

    return edit


def both(*edits: Edit) -> Edit:
    def edit(cwd: Path) -> None:
        for e in edits:
            e(cwd)

    return edit


class EditingAgent:
    """Applies one scripted edit per call."""

    def __init__(self, *edits: Edit) -> None:
        self.edits = list(edits)
        self.prompts: list[str] = []
        self.roles: list[str] = []

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        roles = {
            "reproduce": Reproduced.model_json_schema(),
            "implement": Implemented.model_json_schema(),
        }
        self.roles.append(next(r for r, s in roles.items() if s == schema))
        self.prompts.append(prompt)
        self.edits.pop(0)(cwd)
        return AgentRun(
            output={"summary": f"try {len(self.prompts)}"},
            model="fake",
            cost_usd=0.1,
            turns=2,
            latency_ms=5,
        )


class Script:
    """An editing agent: one (edit, answer) per call."""

    def __init__(self, *steps: tuple[Edit | None, Mapping[str, object]]) -> None:
        self.steps = list(steps)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        self.prompts.append(prompt)
        edit, answer = self.steps.pop(0)
        if edit is not None:
            edit(cwd)
        output = dict(answer)
        return AgentRun(output=output, model="f", cost_usd=0.1, turns=1, latency_ms=1)


class FakeAgent:
    def __init__(self, output: Spec) -> None:
        self.output = output
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        self.calls.append((prompt, cwd))
        assert schema["additionalProperties"] is False
        return AgentRun(
            output=self.output.model_dump(),
            model="fake",
            cost_usd=0.01,
            turns=3,
            latency_ms=10,
        )


class Reviewer:
    """Answers with a fixed review and keeps the prompt."""

    def __init__(self, answer: Reviewed) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        assert schema["additionalProperties"] is False
        self.prompts.append(prompt)
        return AgentRun(
            output=self.answer.model_dump(),
            model="fake",
            cost_usd=0.03,
            turns=4,
            latency_ms=3000,
        )


class Reviews(Reviewer):
    """One review per call, in order."""

    def __init__(self, *answers: Mapping[str, object]) -> None:
        super().__init__(reviewed())
        self.answers = [reviewed(**dict(a)) for a in answers]

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        self.answer = self.answers.pop(0)
        return super().__call__(prompt, schema, cwd)


class Reader:
    """The read-only agent: a spec when asked to refine, else a review."""

    def __init__(self, s: Spec) -> None:
        self.refiner, self.reviewer = FakeAgent(s), Reviewer(reviewed())

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        if schema == Spec.model_json_schema():
            return self.refiner(prompt, schema, cwd)
        return self.reviewer(prompt, schema, cwd)


def loop(
    tmp_path: Path,
    agent: EditingAgent,
    sandbox: HostSandbox | None = None,
    planted: bool = True,
) -> tuple[LoopResult, Path, Path]:
    clone, repro, commit = setup(tmp_path)
    worktree = tmp_path / "work" / "w"
    result = run_loop(
        repo="o/r",
        number=1,
        commit=commit,
        spec=spec("src/a.ts", tests=("src/a.test.sh",)),
        clone=clone,
        worktree=worktree,
        repro_file=repro if planted else None,
        rules=RULES if planted else AGENT_RULES,
        agent=agent,
        sandbox=sandbox or HostSandbox(),
    )
    return result, worktree, clone


def deps(tmp_path: Path, editor: EditingAgent, criteria: list[str]) -> Deps:
    clone, _, _ = setup(tmp_path)
    s: Spec = spec("src/a.ts").model_copy(update={"criteria": criteria})
    return Deps(
        clone=clone,
        work_dir=tmp_path / "work",
        rules=AGENT_RULES,
        reader=Reader(s),
        editor=editor,
        sandbox=HostSandbox(),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        lessons_dir=tmp_path / "lessons",
    )
