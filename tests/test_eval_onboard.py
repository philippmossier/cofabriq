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
    assert onboard_eval.load_set(Path("evals/repos.toml"))  # the committed set loads
