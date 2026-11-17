"""Importing a repo's open issues (`fetch-issues --open`, the UI's "Import
open issues"), the GitHub token's source and the warning when it can write,
and the github.com check before any import."""

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cofabriq.issues import token
from cofabriq.issues.fetch import fetch_issues, fetch_open, load_index
from cofabriq.issues.model import Issue
from cofabriq.loop.graph import FlowRunner
from cofabriq.loop.onboard import on_github
from cofabriq.serve import build
from cofabriq.targets import own_target
from tests.builders import REPO, issue
from tests.fakes import RECORDED, page, replay, source
from tests.gitrepo import git, init_repo
from tests.test_issues import ListSource

# ---- the query and the store ---------------------------------------------------


def opened(total: int) -> httpx.Response:
    """Page 1's nodes as the answer to `OPEN_QUERY`, `total` open in all."""
    nodes = page(1)["data"]["repository"]["issues"]["nodes"]
    issues = {"totalCount": total, "nodes": nodes}
    data = {"rateLimit": page(1)["data"]["rateLimit"], "repository": {"issues": issues}}
    return httpx.Response(200, json={"data": data})


def test_open_issues_is_one_request_with_the_total() -> None:
    handler, sent = replay(opened(120))

    got, total = source(handler, []).open_issues(50)

    assert (len(got), total) == (2, 120)
    assert sent[0]["variables"] | {"comments": 2} == {
        "owner": "open-webui",
        "name": "open-webui",
        "limit": 50,
        "comments": 2,
    }
    assert "states: OPEN" in sent[0]["query"]
    for bad in (0, 101):
        with pytest.raises(ValueError, match="limit must be 1 to 100"):
            source(handler, []).open_issues(bad)


def test_an_import_into_a_new_store_leaves_the_rest_to_fetch(tmp_path: Path) -> None:
    issues = [
        i.model_copy(update={"repo": RECORDED})
        for i in (issue(1), issue(2), issue(3, reason="completed"), issue(4))
    ]

    got = fetch_open(ListSource(issues), tmp_path, 2)

    assert (got.written, got.unchanged, got.total) == (2, 0, 3)
    repo_dir = tmp_path / "open-webui" / "open-webui"
    assert sorted(load_index(repo_dir)) == [2, 4]  # the newest open ones
    assert fetch_open(ListSource(issues), tmp_path, 2).unchanged == 2
    # a later full fetch still starts from the beginning: nothing older skipped
    later = ListSource(issues)
    fetch_issues(later, tmp_path)
    assert later.asked == [None]


# ---- the token -----------------------------------------------------------------


def ran(stdout: str = "", fail: BaseException | None = None) -> token.Run:
    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if fail is not None:
            raise fail
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


def test_gh_is_asked_only_when_opted_in_and_fails_quietly() -> None:
    assert token.gh_token(ran("gho_abc\n")) == "gho_abc"
    assert token.gh_token(ran(fail=FileNotFoundError("gh"))) is None
    not_logged_in = subprocess.CalledProcessError(1, ["gh"], stderr="not logged in")
    assert token.gh_token(ran(fail=not_logged_in)) is None

    def gh() -> str:
        return "gho_abc"

    assert token.resolve("github_pat_x", True, gh) == ("github_pat_x", "env")
    assert token.resolve(None, False, gh) == (None, None)  # not opted in
    assert token.resolve(None, True, gh) == ("gho_abc", "gh")
    assert token.resolve(None, True, lambda: None) == (None, None)


def scopes_of(headers: dict[str, str]) -> list[str] | None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer t"
        return httpx.Response(200, headers=headers, json={})

    return token.scopes("t", httpx.MockTransport(handle))


def test_a_token_that_can_write_is_named_one_that_cant_tell_is_not() -> None:
    gh = token.access("gh", scopes_of({"X-OAuth-Scopes": "gist, read:org, repo"}))
    assert (gh.scopes, gh.can_write) == (["gist", "read:org", "repo"], True)
    assert "can write to your repositories (scope repo)" in gh.warning

    read = token.access("env", scopes_of({"X-OAuth-Scopes": "read:org"}))
    assert (read.can_write, read.warning) == (False, "")
    # a fine-grained token lists no scopes: can't tell, no warning
    fine = token.access("env", scopes_of({}))
    assert (fine.scopes, fine.can_write, fine.warning) == (None, None, "")
    assert token.access(None, None).source is None


# ---- github.com, and the route --------------------------------------------------


LOOP = """[loop]
image = "node:24-slim"
install = "true"
repro_dir = "src"
repro = "sh"
checks = []
"""


def own(tmp_path: Path, remote: str | None) -> Path:
    """An own target for REPO whose `[clone]` is a local clone with `remote`
    as its origin."""
    clone = init_repo(tmp_path / "mine", {"a.txt": "a\n"})
    if remote is not None:
        git(clone, "remote", "add", "origin", remote)
    targets = tmp_path / "targets"
    path = own_target(targets, REPO)
    path.parent.mkdir(parents=True)
    path.write_text(f'[clone]\npath = "{clone}"\nbranch = "main"\n\n{LOOP}')
    return targets


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/acme/app.git", True),
        ("git@github.com:acme/app.git", True),
        ("ssh://git@github.com/acme/app", True),
        ("https://gitlab.com/acme/app.git", False),
        ("https://github.com.evil.dev/acme/app.git", False),
        ("https://github.com/acme/other.git", False),  # another repo's name
        (None, False),  # no origin: `local/<folder>`
    ],
)
def test_only_a_clone_from_github_com_imports(
    tmp_path: Path, remote: str | None, expected: bool
) -> None:
    assert on_github(own(tmp_path, remote), REPO) is expected


def test_a_repo_without_clone_is_named_by_github(tmp_path: Path) -> None:
    assert on_github(tmp_path, "microsoft/vscode") is True


class OpenSource:
    """The import's GitHub source: `total` open, the first `limit` given."""

    def __init__(self, issues: list[Issue], total: int) -> None:
        self.repo, self._issues, self._total = REPO, issues, total
        self.asked: list[int] = []

    def open_issues(self, limit: int) -> tuple[list[Issue], int]:
        self.asked.append(limit)
        return self._issues[:limit], self._total


def importer(tmp_path: Path, remote: str, found: OpenSource | None) -> TestClient:
    targets = own(tmp_path, remote)
    flows = FlowRunner("", lambda repo, spent: None)  # pyright: ignore[reportArgumentType]
    (tmp_path / "tickets" / "acme" / "app").mkdir(parents=True)  # listed: a store
    return TestClient(
        build(
            tmp_path,
            tmp_path,
            tmp_path,
            None,
            flows=flows,
            issues_dir=tmp_path / "issues",
            targets_dir=targets,
            tickets_dir=tmp_path / "tickets",
            github=lambda repo: found,  # pyright: ignore[reportArgumentType]
        )
    )


def proven(tmp_path: Path) -> None:
    """`repo check`'s proof for the own target as it is now."""
    from cofabriq.loop import onboard
    from cofabriq.loop.run import CheckResult

    targets = tmp_path / "targets"
    proof = onboard.Checked(
        repo=REPO,
        commit="c",
        config=onboard.config_hash(own_target(targets, REPO)),
        at="now",
        install=CheckResult(name="install", ok=True, seconds=1, output=""),
        checks=[CheckResult(name="test", ok=True, seconds=1, output="")],
        canaries=[onboard.Canary(suite="sh", red=True, green=True, output="")],
        seconds=1,
    )
    onboard.proof_path(targets, REPO).write_text(proof.model_dump_json())


def test_more_open_than_the_cap_asks_first_then_imports(tmp_path: Path) -> None:
    found = OpenSource([issue(9), issue(8), issue(7)], total=120)
    c = importer(tmp_path, "git@github.com:acme/app.git", found)
    proven(tmp_path)
    url = f"/api/repos/{REPO}/import"

    asked = c.post(url, json={"limit": 2}).json()

    assert asked == {"total": 120, "written": 0, "unchanged": 0, "asked": True}
    assert not (tmp_path / "issues").exists()  # nothing stored yet
    done = c.post(url, json={"limit": 2, "confirm": True}).json()
    assert (done["written"], done["asked"]) == (2, False)
    assert sorted(load_index(tmp_path / "issues" / "acme" / "app")) == [8, 9]
    assert c.get("/api/repos").json()[0]["on_github"] is True
    assert c.post(url, json={"limit": 101}).status_code == 422


def test_no_import_off_github_or_without_a_token(tmp_path: Path) -> None:
    gitlab = importer(tmp_path / "a", "https://gitlab.com/acme/app.git", None)
    proven(tmp_path / "a")
    refused = gitlab.post(f"/api/repos/{REPO}/import", json={})
    assert refused.status_code == 409
    assert "not on github.com" in refused.json()["detail"]

    no_token = importer(tmp_path / "b", "https://github.com/acme/app.git", None)
    proven(tmp_path / "b")
    assert no_token.post(f"/api/repos/{REPO}/import", json={}).status_code == 503
