import json
from pathlib import Path

import pytest

from cofabriq.loop.engine import (
    STOP_ENV,
    AgentError,
    ApiKey,
    ClaudeCode,
    Guard,
    Guarded,
    Halted,
    stop_reason,
)
from cofabriq.loop.run import run_loop
from tests.test_loop import AGENT_RULES, REPRO, EditingAgent, HostSandbox, setup, write
from tests.test_refine import spec

SCHEMA: dict[str, object] = {"type": "object"}


def fake_claude(tmp_path: Path, cost: float) -> Path:
    """A `claude` that records its argv and answers like a successful run."""
    answer = {
        "subtype": "success",
        "structured_output": {"summary": "s"},
        "total_cost_usd": cost,
        "num_turns": 2,
    }
    script = tmp_path / "claude"
    script.write_text(
        f"#!/bin/sh\necho \"$@\" >> {tmp_path / 'argv'}\necho '{json.dumps(answer)}'\n"
    )
    script.chmod(0o755)
    return script


def agent(tmp_path: Path, cost: float = 0.4) -> ClaudeCode:
    return ClaudeCode(
        auth=ApiKey("k"),
        binary=str(fake_claude(tmp_path, cost)),
        stop_file=tmp_path / "STOP",
    )


def test_every_run_has_a_turn_cap(tmp_path: Path) -> None:
    cmd = agent(tmp_path).command(SCHEMA, tmp_path)
    assert cmd[cmd.index("--max-turns") + 1] == "40"


def test_a_stop_file_halts_before_the_agent_starts(tmp_path: Path) -> None:
    a = agent(tmp_path)
    a("p", SCHEMA, tmp_path)
    (tmp_path / "STOP").write_text("")
    with pytest.raises(Halted, match="STOP exists"):
        a("p", SCHEMA, tmp_path)
    assert len((tmp_path / "argv").read_text().splitlines()) == 1  # not started
    assert not issubclass(Halted, AgentError)  # no retry catches it


def test_the_env_var_halts_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STOP_ENV, "1")
    assert stop_reason(tmp_path / "none") == f"{STOP_ENV} is set"
    monkeypatch.setenv(STOP_ENV, "0")
    assert stop_reason(tmp_path / "none") is None


def test_a_run_budget_clamps_each_call_and_halts_when_spent(tmp_path: Path) -> None:
    guarded = Guarded(agent(tmp_path, cost=0.4), Guard(1.0))
    guarded("p", SCHEMA, tmp_path)  # 0.4 spent
    guarded("p", SCHEMA, tmp_path)  # 0.8 spent
    guarded("p", SCHEMA, tmp_path)  # starts with 0.2 left, clamped
    with pytest.raises(Halted, match="run budget"):
        guarded("p", SCHEMA, tmp_path)
    budgets = [
        line.split("--max-budget-usd ")[1].split()[0]
        for line in (tmp_path / "argv").read_text().splitlines()
    ]
    assert [round(float(b), 2) for b in budgets] == [1.0, 0.6, 0.2]


def test_the_loop_ends_halted_not_escalated(tmp_path: Path) -> None:
    clone, _, commit = setup(tmp_path)

    class Stopping(EditingAgent):
        def __call__(self, prompt: str, schema: dict[str, object], cwd: Path):  # type: ignore[override]
            if len(self.prompts) == 1:  # the builder's first call
                raise Halted("stopped")
            return super().__call__(prompt, schema, cwd)

    result = run_loop(
        repo="o/r",
        number=1,
        commit=commit,
        spec=spec("src/a.ts"),
        clone=clone,
        worktree=tmp_path / "work" / "w",
        repro_file=None,
        rules=AGENT_RULES,
        agent=Stopping(write("src/a.test.sh", REPRO)),
        sandbox=HostSandbox(),
    )
    assert result.status == "halted"


def test_a_failed_run_still_counts_against_the_budget(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    failed = {
        "subtype": "error_max_budget_usd",
        "is_error": True,
        "total_cost_usd": 0.7,
    }
    script.write_text(f"#!/bin/sh\necho '{json.dumps(failed)}'\n")
    script.chmod(0o755)
    guard = Guard(1.0)
    a = ClaudeCode(auth=ApiKey("k"), binary=str(script), stop_file=tmp_path / "S")
    with pytest.raises(AgentError):
        Guarded(a, guard)("p", SCHEMA, tmp_path)
    assert guard.spent_usd == 0.7


def test_a_halted_run_never_shadows_an_older_result(tmp_path: Path) -> None:
    from cofabriq.eval.loop import Judged
    from cofabriq.eval.review import load_runs
    from tests.test_loop import _result  # pyright: ignore[reportPrivateUsage]

    old = Judged(result=_result("green", 1, ["src/a.ts"]))
    new = Judged(result=_result("halted", 0, []))
    (tmp_path / "eval-20261018T100000.json").write_text(
        json.dumps({"bug": old.model_dump(mode="json")})
    )
    (tmp_path / "eval-20261018T110000.json").write_text(
        json.dumps({"bug": new.model_dump(mode="json")})
    )
    assert load_runs(tmp_path, "eval")["bug"].result.status == "green"
