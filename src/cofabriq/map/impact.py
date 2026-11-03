"""Impact: which components a change to an issue (or a plan) will touch.

No code is read. The earlier issues most similar to this one, among those
with a fix on the map (pgvector), vote with the components their fixes
touched ("fixes #N" commits), weighted by similarity. Separately, what
usually changes with the top components (co-change) is listed as the blast
radius: on vscode, mixing it into the ranking did not help. Measured by
`cofabriq eval impact`, which calls `vote` and `spread` from here.
"""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from neo4j import Driver
from pydantic import BaseModel

from cofabriq.context.ingest.embed import Embed
from cofabriq.context.store import postgres
from cofabriq.issues import vectors
from cofabriq.issues.fetch import load_issue
from cofabriq.issues.triage import NotReady
from cofabriq.map import store as map_store

NEIGHBOURS = 30  # earlier fixed issues that vote (kNN), as measured
SPREAD = 3  # top components whose co-change partners are added

type Ranked = list[tuple[str, float]]  # component, score; best first


def vote(
    neighbours: Sequence[tuple[int, float]], fixed: Callable[[int], list[str]]
) -> tuple[Ranked, dict[str, list[int]]]:
    """Each neighbour (number, similarity) adds its similarity to every
    component its fix touched. Also returns which issues voted for what."""
    votes: dict[str, float] = {}
    via: dict[str, list[int]] = {}
    for number, score in neighbours:
        for component in fixed(number):
            votes[component] = votes.get(component, 0.0) + score
            via.setdefault(component, []).append(number)
    return sorted(votes.items(), key=lambda v: (-v[1], v[0])), via


def spread(
    ranked: Ranked, partners: Callable[[str], list[tuple[str, float]]]
) -> Ranked:
    """Add the co-change partners of the SPREAD best components, each worth
    its parent's score times the share of the parent's commits it shares."""
    scores = dict(ranked)
    for component, score in ranked[:SPREAD]:
        for partner, share in partners(component):
            scores[partner] = scores.get(partner, 0.0) + score * share
    return sorted(scores.items(), key=lambda v: (-v[1], v[0]))


class Touch(BaseModel):
    component: str
    score: float  # summed similarity of the issues that voted (relative only)
    via: list[int]  # earlier issues whose fix touched it; empty: co-change only


class Impact(BaseModel):
    repo: str
    map_rev: str  # the map's commit: co-change and fixes known up to it
    components: list[Touch]  # likely touched, best first
    blast: list[Touch]  # not voted for, but usually changes with the top ones
    neighbours: list[tuple[int, float]]  # earlier fixed issues looked at


def assemble(
    repo: str,
    map_rev: str,
    neighbours: Sequence[tuple[int, float]],
    fixed: Mapping[int, list[str]],
    partners: Callable[[str], list[tuple[str, float]]],
    k: int,
) -> Impact:
    ranked, via = vote(neighbours, lambda n: fixed.get(n, []))
    voted = dict(ranked)
    blast = [(c, s) for c, s in spread(ranked, partners) if c not in voted]
    return Impact(
        repo=repo,
        map_rev=map_rev,
        components=[
            Touch(component=c, score=round(s, 3), via=via[c]) for c, s in ranked[:k]
        ],
        blast=[Touch(component=c, score=round(s, 3), via=[]) for c, s in blast[:k]],
        neighbours=[(n, round(s, 3)) for n, s in neighbours],
    )


def impact_fn(
    conn: postgres.Conn, driver: Driver, embed: Embed, issues_dir: Path
) -> Callable[[str, int | None, str | None, int], Impact]:
    """(repo, issue number or None, plan text or None, k) -> Impact from the
    stores (the CLI's `impact` and the MCP tool). An issue must be fetched
    and embedded; it only sees fixes committed before it was filed. A plan
    sees everything on the map."""

    def run(repo: str, number: int | None, plan: str | None, k: int) -> Impact:
        rev = map_store.rev(driver, repo)
        if rev is None:
            raise NotReady(f"no map for {repo}: run map build")
        if number is not None:
            issue = load_issue(issues_dir, repo, number)
            if issue is None or vectors.embedding(conn, repo, number) is None:
                raise NotReady(
                    f"{repo}#{number} is not fetched and embedded: run triage first"
                )
            before = issue.created_at
            among = map_store.fixed_issues(driver, repo, before)
            hits = vectors.nearest(conn, repo, number, NEIGHBOURS, before, among)
        elif plan is not None:
            before = None
            among = map_store.fixed_issues(driver, repo, None)
            [vector] = embed([plan[: vectors.MAX_BODY]])
            hits = vectors.nearest_to(conn, repo, vector, NEIGHBOURS, among)
        else:
            raise ValueError("an issue number or a plan")
        neighbours = [(h.number, h.score) for h in hits]
        fixed = map_store.fixed(driver, repo, [n for n, _ in neighbours], before)

        def partners(component: str) -> list[tuple[str, float]]:
            found = map_store.changes_with(driver, repo, component, 10)
            return [(p.name, p.share) for p in found]

        return assemble(repo, rev, neighbours, fixed, partners, k)

    return run
