import json
from pathlib import Path

from fastapi.testclient import TestClient

from cofabriq.loop.run import Evidence, Suite, collect_evidence
from cofabriq.serve import build
from tests.test_loop import AGENT_RULES, HostSandbox

# the "browser test" writes two screenshots, the way Playwright's outputDir does
SHOTS = (
    "mkdir -p out/a out/b && printf png > out/a/shot.png"
    " && printf png > out/b/shot.png &&"
)
RULES = AGENT_RULES.model_copy(
    update={
        "suites": [
            Suite(name="unit", suffix=".test.sh", cmd="sh", dir="src/repro"),
            Suite(name="ui", suffix=".spec.sh", cmd="sh", dir="e2e/repro"),
        ],
        "evidence": Evidence(suite="ui", cmd=f"{SHOTS} true", glob="out/**/*.png"),
    }
)


def test_only_the_evidence_suites_tests_get_screenshots(tmp_path: Path) -> None:
    worktree, dest = tmp_path / "w", tmp_path / "runs" / "r1"
    worktree.mkdir()
    sandbox = HostSandbox()
    names = collect_evidence(
        RULES, worktree, ["e2e/a.spec.sh", "src/b.test.sh"], sandbox, dest
    )
    assert names == ["1-a.png", "2-b.png"]
    assert (dest / "1-a.png").read_text() == "png"
    assert sandbox.calls[0][0].endswith(
        "true e2e/a.spec.sh"
    )  # the unit test isn't rerun
    assert sandbox.calls[0][1] is False  # no network
    assert collect_evidence(RULES, worktree, ["src/b.test.sh"], sandbox, dest) == []
    assert (
        collect_evidence(AGENT_RULES, worktree, ["e2e/a.spec.sh"], sandbox, dest) == []
    )


def test_the_viewer_serves_a_runs_screenshots_only(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    (runs / "r1" / "1-a.png").write_text("png")
    (tmp_path / "secret.png").write_text("no")
    (runs / "r1.json").write_text(json.dumps({}))
    c = TestClient(build(tmp_path, runs, tmp_path, None))
    assert c.get("/api/runs/r1/evidence/1-a.png").text == "png"
    assert c.get("/api/runs/r1/evidence/..%2F..%2Fsecret.png").status_code == 404
    assert c.get("/api/runs/r1/evidence/nope.png").status_code == 404
    assert c.get("/api/runs/r2/evidence/1-a.png").status_code == 404


def test_the_pr_body_says_where_the_screenshots_are() -> None:
    from cofabriq.loop.pr import RiskRules, draft
    from cofabriq.map.components import ComponentRules
    from tests.test_loop import _result  # pyright: ignore[reportPrivateUsage]
    from tests.test_refine import spec

    pr = draft(
        issue_title="t",
        spec=spec("src/a.ts"),
        result=_result("green", 1, ["src/a.ts"]),
        tests=AGENT_RULES.tests,
        risk_rules=RiskRules(),
        components=ComponentRules(),
        review=None,
        evidence=2,
    )
    assert "**Screenshots:** 2 from the browser test after the change" in pr.body
