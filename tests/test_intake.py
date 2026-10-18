import httpx

from cofabriq.issues.model import Issue, IssueRef
from cofabriq.loop.engine import AgentRun
from cofabriq.loop.intake import blockers, not_ready, slices
from cofabriq.loop.refine import Refined
from tests.test_dedupe import REPO, issue
from tests.test_issues import one, page, replay, source
from tests.test_refine import spec


def ref(n: int, repo: str = REPO) -> IssueRef:
    return IssueRef(repo=repo, number=n)


def blocked(n: int, body: str = "", native: list[int] | None = None) -> Issue:
    t = issue(n, body=body)
    return t.model_copy(update={"blocked_by": [ref(b) for b in native or []]})


def issues_by_ref(*issues: Issue) -> dict[IssueRef, Issue]:
    known: dict[IssueRef, Issue] = {}
    for i in issues:
        known[ref(i.number)] = i
    return known


def test_blockers_native_and_from_the_body() -> None:
    t = blocked(
        5,
        body="Part 2.\n\nBlocked by #3, #4 and Other/Repo#9\n- blocked by: #3\n#7 too",
        native=[2, 3],
    )
    assert blockers(t) == [ref(2), ref(3), ref(4), ref(9, "other/repo")]
    assert blockers(blocked(5, body="Blocked by #5")) == []  # itself


def test_slice_states() -> None:
    closed = issue(2, reason="completed")
    tickets = [
        issue(1, reason="completed"),
        blocked(3, native=[2]),  # blocker done
        blocked(4, native=[3]),  # blocker open
        blocked(6, native=[99]),  # blocker unknown: open
    ]
    known = issues_by_ref(closed, tickets[1])
    states = [(s.number, s.state) for s in slices(tickets, known, [])]
    assert states == [(1, "done"), (3, "ready"), (4, "blocked"), (6, "blocked")]
    waiting = slices([tickets[1]], known, [40])[0]
    assert waiting.state == "waiting" and "#40" in waiting.reason


def test_not_planned_blocker_does_not_count_as_done() -> None:
    dropped = issue(2, reason="not_planned")
    (s,) = slices([blocked(3, native=[2])], issues_by_ref(dropped), [])
    assert s.state == "blocked"


def refined(route: str = "feature", **update: object) -> Refined:
    s = spec("src/a.ts", "src/new.ts").model_copy(update=update)
    run = AgentRun(output={}, model="m", cost_usd=0, turns=1, latency_ms=1)
    return Refined.model_validate(
        {
            "repo": REPO,
            "number": 1,
            "commit": "c" * 40,
            "route": route,
            "spec": s.model_dump(),
            "unknown_paths": [],
            "run": run.model_dump(),
        }
    )


def test_readiness_reasons() -> None:
    tracked = {"src/a.ts"}
    reasons = not_ready(refined(open_questions=["which?"], size="L"), tracked)
    assert reasons == [
        "no acceptance criteria a test can check",
        "files not in the repo: src/new.ts",
        "size L: split it into tickets in your planning session",
    ]
    ok = refined(criteria=["f(1) is 2"]).spec.model_copy()
    ok.files[1].why = "new module for the feature"
    assert not_ready(refined(criteria=["f(1) is 2"], files=ok.files), tracked) == []
    assert not_ready(refined("bug", files=ok.files), tracked) == []


def test_github_reads_native_blockers() -> None:
    node = page(1)["data"]["repository"]["issues"]["nodes"][0]
    node = node | {
        "blockedBy": {"nodes": [{"number": 3, "repository": {"nameWithOwner": REPO}}]}
    }
    handler, sent = replay(one(node))
    found = source(handler, []).issue(node["number"])
    assert found is not None and found.blocked_by == [ref(3)]
    assert "blockedBy(first: 10)" in sent[0]["query"]


def test_open_pulls_keeps_cofabriq_branches() -> None:
    pulls = {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {"number": 9, "headRefName": "cofabriq/5-x-abc1234"},
                        {"number": 8, "headRefName": "feature/other"},
                    ]
                }
            }
        }
    }
    handler, _ = replay(httpx.Response(200, json=pulls))
    assert source(handler, []).open_pulls("cofabriq/") == [9]
