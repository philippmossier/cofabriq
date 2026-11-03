from pathlib import Path

from cofabriq.issues.triage import load_components
from cofabriq.map.components import load_rules
from cofabriq.targets import PLAYGROUND, clone_dir, repo_slug, target_table


def test_a_table_is_none_when_the_file_or_the_table_is_missing(
    tmp_path: Path,
) -> None:
    assert target_table(tmp_path, "acme/app", "loop") is None
    (tmp_path / "acme-app.toml").write_text('[risk]\nhigh = ["auth"]\n', "utf-8")
    assert target_table(tmp_path, "acme/app", "loop") is None
    assert target_table(tmp_path, "acme/app", "risk") == {"high": ["auth"]}


def test_the_slug_and_the_clone_keep_their_stored_paths() -> None:
    assert repo_slug("acme/app") == "acme-app"
    assert clone_dir(Path("data/clones"), "acme/app") == Path("data/clones/acme-app")


def test_playground_triage_components_are_the_map_components() -> None:
    # triage picks from [components], the risk tier reads [map.named]: a name
    # in one but not the other is a component triage can name and the map
    # never finds (or the other way round)
    targets = Path("targets")
    triage = load_components(targets, PLAYGROUND)
    named = load_rules(targets, PLAYGROUND).named
    assert set(triage) == set(named)
