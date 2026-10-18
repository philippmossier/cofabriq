import json
from pathlib import Path

from fastapi.testclient import TestClient

from cofabriq.eval.loop import Judged
from cofabriq.loop.run import CheckResult
from cofabriq.serve import build
from tests.test_intake import refined
from tests.test_loop import _result  # pyright: ignore[reportPrivateUsage]


def client(tmp_path: Path, ui: Path | None = None) -> TestClient:
    refine, runs = tmp_path / "refine", tmp_path / "runs"
    refine.mkdir()
    runs.mkdir()
    spec = refined(criteria=["f(1) is 2"], open_questions=["which?"])
    (refine / "features-20261018T010203.json").write_text(
        json.dumps({"k": spec.model_dump(mode="json"), "old": {"shape": 1}})
    )
    judged = Judged(
        result=_result("green", 2, ["src/a.ts"]),
        judge=CheckResult(name="judge", ok=True, seconds=1, output=""),
    )
    (runs / "eval-20261018T010203.json").write_text(
        json.dumps({"bug": judged.model_dump(mode="json")})
    )
    (tmp_path / "secret.json").write_text("{}")
    return TestClient(build(refine, runs, tmp_path / "clones", ui))


def test_specs_list_and_view(tmp_path: Path) -> None:
    c = client(tmp_path)
    (summary,) = c.get("/api/specs").json()  # the old shape is left out
    assert summary["key"] == "k" and summary["criteria"] == 1
    assert summary["open_questions"] == 1
    assert summary["not_ready"] is None  # no clone to check paths against
    assert summary["made_at"] == "2026-10-18T01:02:03Z"
    view = c.get("/api/specs/features-20261018T010203/k").json()
    assert view["criteria"] == ["f(1) is 2"] and view["route"] == "feature"


def test_runs_list_and_trace(tmp_path: Path) -> None:
    c = client(tmp_path)
    (run,) = c.get("/api/runs").json()
    assert (run["kind"], run["status"], run["judge"], run["tries"]) == (
        "eval",
        "green",
        True,
        2,
    )
    trace = c.get("/api/runs/eval-20261018T010203/bug").json()
    assert [s["phase"] for s in trace["steps"]] == ["build", "build"]
    assert trace["gate"] is None and trace["rounds"] == []


def test_only_files_in_the_folder(tmp_path: Path) -> None:
    c = client(tmp_path)
    for url in [
        "/api/specs/..%2Fsecret/k",
        "/api/specs/../secret/k",
        "/api/specs/nope/k",
        "/api/specs/features-20261018T010203/nope",
        "/api/runs/eval-20261018T010203/nope",
    ]:
        assert c.get(url).status_code == 404, url


def test_the_built_ui_answers_every_other_path(tmp_path: Path) -> None:
    ui = tmp_path / "dist"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<div id=root></div>")
    c = client(tmp_path, ui)
    assert c.get("/runs/x/y").text == "<div id=root></div>"
    assert c.get("/api/runs").status_code == 200
    (ui / "favicon.svg").write_text("<svg/>")
    assert c.get("/favicon.svg").text == "<svg/>"
    assert c.get("/../secret.json").text == "<div id=root></div>"


def test_no_route_changes_anything(tmp_path: Path) -> None:
    app = build(tmp_path, tmp_path, tmp_path, None)
    paths: dict[str, dict[str, object]] = app.openapi()["paths"]
    assert {m for ops in paths.values() for m in ops} == {"get"}


def test_the_committed_schema_is_current(tmp_path: Path) -> None:
    """ui/openapi.json is what the UI's client is generated from: refresh it
    with `cofabriq openapi` when a response model changes."""
    committed = Path(__file__).parent.parent / "ui" / "openapi.json"
    schema = build(tmp_path, tmp_path, tmp_path, None).openapi()
    assert json.loads(committed.read_text("utf-8")) == schema
