from pathlib import Path

import pytest

from cofabriq.eval.features import (
    load_features,
    patched_files,
    removal_path,
    ticket,
)

PLAYGROUND = Path(__file__).parent.parent / "playground"


def test_answer_key_matches_the_patches() -> None:
    features = load_features(PLAYGROUND / "features.toml")
    assert len(features) == 9
    for f in features:
        assert removal_path(PLAYGROUND / "features.toml", f.key).is_file()


def test_patched_files_reads_the_new_side() -> None:
    patch = "--- a/x.ts\n+++ b/x.ts\n@@ -1 +1 @@\n-a\n+b\n+++ not a header\n"
    assert patched_files(patch) == {"x.ts"}


def test_a_file_outside_the_patch_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "features" / "remove").mkdir(parents=True)
    (tmp_path / "features" / "remove" / "k.patch").write_text(
        "--- a/src/a.ts\n+++ b/src/a.ts\n--- a/src/a.test.ts\n+++ b/src/a.test.ts\n"
    )
    (tmp_path / "features.toml").write_text(
        '[[feature]]\nkey = "k"\nsize = "S"\nfiles = ["src/b.ts"]\n'
        'tests = ["src/a.test.ts"]\ntitle = "t"\nbody = "b"\n'
    )
    with pytest.raises(ValueError, match=r"not in the patch: src/b\.ts"):
        load_features(tmp_path / "features.toml")


def test_ticket_is_an_open_issue_without_a_number() -> None:
    f = load_features(PLAYGROUND / "features.toml")[0]
    issue = ticket(f, "owner/repo")
    assert (issue.number, issue.state, issue.title) == (0, "open", f.title)
    assert issue.body == f.body.strip()
