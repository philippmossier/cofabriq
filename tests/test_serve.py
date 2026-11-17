import json
from pathlib import Path

from fastapi.testclient import TestClient

from cofabriq.eval.loop import Judged
from cofabriq.git import git
from cofabriq.loop.graph import FlowRunner
from cofabriq.loop.run import CheckResult
from cofabriq.serve import build
from cofabriq.targets import clone_dir
from tests.builders import loop_result, refined
from tests.gitrepo import init_repo


def client(tmp_path: Path, ui: Path | None = None) -> TestClient:
    refine, runs = tmp_path / "refine", tmp_path / "runs"
    refine.mkdir()
    runs.mkdir()
    spec = refined(criteria=["f(1) is 2"], open_questions=["which?"])
    (refine / "features-20261018T010203.json").write_text(
        json.dumps({"k": spec.model_dump(mode="json"), "old": {"shape": 1}})
    )
    judged = Judged(
        result=loop_result("green", 2, ["src/a.ts"]),
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
    assert run["title"] == ""  # an eval case is no issue in the store
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


def test_answer_start_and_a_ticket_are_the_only_write_routes(tmp_path: Path) -> None:
    app = build(tmp_path, tmp_path, tmp_path, None)
    paths: dict[str, dict[str, object]] = app.openapi()["paths"]
    assert {m for ops in paths.values() for m in ops} == {"get"}
    # with the browser gate: answer a gate, start a flow, write a local
    # ticket; nothing else writes
    flows = FlowRunner("", lambda repo, spent: None)  # pyright: ignore[reportArgumentType]
    gated: dict[str, dict[str, object]] = build(
        tmp_path, tmp_path, tmp_path, None, flows=flows
    ).openapi()["paths"]
    writes = [p for p, ops in gated.items() if set(ops) - {"get"}]
    assert writes == [
        "/api/flows/{owner}/{name}/{ticket}/answer",
        "/api/flows/{owner}/{name}/{ticket}/start",
        "/api/tickets/{owner}/{name}",
    ]


def test_the_committed_schema_is_current(tmp_path: Path) -> None:
    """ui/openapi.json is what the UI's client is generated from: refresh it
    with `cofabriq openapi` when a response model changes."""
    committed = Path(__file__).parent.parent / "ui" / "openapi.json"
    flows = FlowRunner("", lambda repo, spent: None)  # pyright: ignore[reportArgumentType]
    schema = build(tmp_path, tmp_path, tmp_path, None, flows=flows).openapi()
    assert json.loads(committed.read_text("utf-8")) == schema


def test_served_on_a_port_it_is_read_only_and_checks_the_host(tmp_path: Path) -> None:
    app = build(tmp_path, tmp_path, tmp_path, None, port=8765)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/runs").status_code == 200
    assert c.post("/api/runs", json={}).status_code == 405
    other = TestClient(app, base_url="http://evil.example:8765")
    assert other.get("/api/runs").status_code == 403


def test_code_lines_come_from_the_clone_at_the_commit_only(tmp_path: Path) -> None:
    body = "".join(f"line {n}\n" for n in range(1, 41))
    clone = init_repo(clone_dir(tmp_path / "clones", "acme/app"), {"src/a.ts": body})
    commit = git(clone, "rev-parse", "HEAD").strip()
    c = TestClient(build(tmp_path, tmp_path, tmp_path / "clones", None))
    url = f"/api/code/acme/app?commit={commit}"

    around = c.get(f"{url}&path=src/a.ts&line=20").json()
    span = c.get(f"{url}&path=src/a.ts&line=10&end=30").json()
    top = c.get(f"{url}&path=src/a.ts").json()

    assert (around["start"], len(around["lines"]), around["total"]) == (12, 17, 40)
    assert around["lines"][8] == "line 20"
    assert (span["start"], span["lines"][-1]) == (2, "line 38")  # 8 around the range
    assert (top["start"], top["lines"][0]) == (1, "line 1")
    for bad in ("../../etc/passwd", "src/nope.ts", ".git/config"):
        assert c.get(f"{url}&path={bad}").status_code == 404
    assert c.get("/api/code/acme/app?commit=HEAD&path=src/a.ts").status_code == 404
