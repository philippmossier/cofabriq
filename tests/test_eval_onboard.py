from pathlib import Path

from cofabriq.eval import onboard as onboard_eval
from cofabriq.loop import onboard
from cofabriq.loop.run import CheckResult


def result(name: str, ok: bool, output: str = "") -> CheckResult:
    return CheckResult(name=name, ok=ok, seconds=1, output=output)


def checked(**update: object) -> onboard.Checked:
    base = onboard.Checked(
        repo="acme/app",
        commit="c",
        config="",
        at="now",
        install=result("install", True),
        checks=[result("lint", True), result("test", False, "x" * 700 + "boom")],
        canaries=[
            onboard.Canary(suite="unit", red=True, green=True, output=""),
            onboard.Canary(suite="e2e", red=False, green=True, output="all passed"),
        ],
        seconds=3,
    )
    return base.model_copy(update=update)


def test_failures_name_each_step_with_the_end_of_its_output() -> None:
    why = onboard_eval.failures(checked())
    assert list(why) == ["test", "canary e2e"]
    assert why["test"].endswith("boom") and len(why["test"]) == onboard_eval.WHY
    assert why["canary e2e"] == "all passed"


def test_a_failed_install_is_the_only_failure() -> None:
    why = onboard_eval.failures(
        checked(
            install=result("install", False, "manifest unknown"), checks=[], canaries=[]
        )
    )
    assert why == {"install": "manifest unknown"}


def test_the_set_is_a_list_of_repos(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    path.write_text('repos = ["acme/app", "acme/lib"]  # two\n')
    assert onboard_eval.load_set(path) == ["acme/app", "acme/lib"]
    assert onboard_eval.load_set(Path("evals/repos.toml"))  # the committed sets load
    held_out = onboard_eval.load_set(Path("evals/repos-heldout.toml"))
    assert held_out and not set(held_out) & set(
        onboard_eval.load_set(Path("evals/repos.toml"))
    )


def scored(repo: str, repairs: int, proven: bool) -> onboard_eval.Scored:
    return onboard_eval.Scored(
        repo=repo,
        commit="c",
        repairs=repairs,
        proven=proven,
        failed=[],
        why={},
        cited=4,
        unchecked=["image"] if repairs == 0 else [],
        cost_usd=0.4,
        turns=10,
        propose_s=60,
        check_s=30,
        rules=onboard.Proposal.model_validate(
            {
                "image": {"value": "x", "source": "", "quote": ""},
                "setup": "",
                "install": {"value": "true", "source": "", "quote": ""},
                "checks": [],
                "suites": [],
                "tests": [],
                "repro_checks": [],
                "protected": [],
                "notes": "",
            }
        ).rules(),
        notes="",
    )


def test_the_summary_keeps_the_unedited_number_and_counts_each_round() -> None:
    t = onboard_eval.summarize(
        [
            scored("a/proven", 0, True),
            scored("a/second", 0, False),
            scored("a/second", 1, True),
            scored("a/never", 0, False),
            scored("a/never", 1, False),
            scored("a/never", 2, False),
        ]
    )
    assert (t.repos, t.proven, t.proven_after) == (3, 1, [1, 2, 2])
    assert t.quotes_found == 0.75  # the first proposals' quotes only
    assert t.cost_usd == 0.8  # 6 proposals over 3 repos
