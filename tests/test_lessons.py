import json
from dataclasses import replace
from pathlib import Path

from cofabriq.loop.engine import AgentRun
from cofabriq.loop.gate import Gated, Proof, Round
from cofabriq.loop.lessons import Proposed, Rule, Signal, collect, propose, render
from cofabriq.loop.review import Finding, Review
from cofabriq.loop.run import CheckResult
from tests.test_loop import _result  # pyright: ignore[reportPrivateUsage]
from tests.test_pr import reviewed

REPO = "o/r"


def gated(text: str) -> Gated:
    finding = Finding(path="src/a.ts", line=1, severity="concern", text=text)
    red = CheckResult(name="proof", ok=False, seconds=1, output="")
    proof = Proof(finding=finding, attempts=[], quote="q", tests=["p.test.sh"], red=red)
    run = AgentRun(output={}, model="m", cost_usd=0, turns=1, latency_ms=1)
    review = Review(reviewed=reviewed(), approved=False, unknown_paths=[], run=run)
    rounds = [Round(review=review, proofs=[proof], attempts=[])]
    return Gated(status="approved", result=_result("green", 1, []), rounds=rounds)


def test_collect_dedupes_findings_and_reads_rejections(tmp_path: Path) -> None:
    runs, lessons = tmp_path / "runs", tmp_path / "lessons"
    runs.mkdir()
    lessons.mkdir()
    case = {
        "key": "k",
        "kind": "wrong",
        "gated": gated("extra guard").model_dump(mode="json"),
    }
    for stamp in ("1", "2"):  # the same finding proven in two eval runs
        (runs / f"gate-2026101{stamp}T000000.json").write_text(json.dumps([case]))
    line = {
        "repo": REPO,
        "number": 7,
        "title": "T",
        "route": "bug",
        "gate": "plan",
        "reason": "wrong file",
    }
    (lessons / "o-r-rejections.jsonl").write_text(json.dumps(line) + "\n")
    signals = collect(runs, lessons, REPO)
    assert [(s.id, s.kind, s.text) for s in signals] == [
        ("s1", "proven finding", "extra guard"),
        ("s2", "rejected plan", "wrong file"),
    ]
    assert collect(runs, lessons, "other/repo") == []


class Proposer:
    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        assert "<signals>\ns1 [proven finding] src/a.ts: x" in prompt
        rules = [
            Rule(rule="Do what the spec says", why="w", signals=["s1"]),
            Rule(rule="Invented", why="w", signals=["s9"]),
            Rule(rule="Uncited", why="w", signals=[]),
        ]
        out = Proposed(rules=rules).model_dump()
        return AgentRun(output=out, model="m", cost_usd=0.1, turns=1, latency_ms=1)


def test_rules_must_cite_known_signals(tmp_path: Path) -> None:
    signals = [
        Signal(id="s1", kind="proven finding", where="src/a.ts", text="x", source="f")
    ]
    draft = propose(REPO, signals, tmp_path, Proposer())
    assert [r.rule for r in draft.rules] == ["Do what the spec says"]
    assert [r.rule for r in draft.dropped] == ["Invented", "Uncited"]
    text = render(draft)
    assert "## 1. Do what the spec says" in text and "- s1 [proven finding]" in text
    assert "Dropped" in text


def test_a_rejection_reason_reaches_the_lessons_file(tmp_path: Path) -> None:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from cofabriq.loop.graph import FlowState, build_graph
    from tests.test_dedupe import issue
    from tests.test_flow_graph import deps
    from tests.test_loop import EditingAgent

    d = deps(tmp_path, EditingAgent(), [])
    graph = build_graph(replace(d, lessons_dir=tmp_path / "l"))
    app = graph.compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "t"}}
    app.invoke(FlowState(issue=issue(3), route="bug"), config)
    app.invoke(
        Command(resume={"approve": False, "reason": "touches the wrong file"}), config
    )
    (line,) = (tmp_path / "l" / "acme-app-rejections.jsonl").read_text().splitlines()
    assert json.loads(line)["reason"] == "touches the wrong file"
    state = FlowState.model_validate(app.get_state(config).values)
    assert state.note == "plan rejected: touches the wrong file"
