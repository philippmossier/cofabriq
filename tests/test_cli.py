"""`--only key,key` on the eval commands: one rule for all of them."""

import pytest
import typer

from cofabriq.cli.common import pick_only

ITEMS = [("a", 1), ("b", 2), ("c", 3)]


def test_without_only_every_item_runs() -> None:
    assert pick_only(ITEMS, None, lambda i: i[0], "bug") == ITEMS


def test_only_keeps_the_items_order() -> None:
    assert pick_only(ITEMS, "c,a", lambda i: i[0], "bug") == [("a", 1), ("c", 3)]


def test_an_unknown_key_exits_and_names_it(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as exit_:
        pick_only(ITEMS, "a,typo", lambda i: i[0], "bug")

    assert exit_.value.exit_code == 1
    assert capsys.readouterr().err == "unknown bug keys: typo\n"
