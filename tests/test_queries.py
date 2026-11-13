"""Every GraphQL query `GitHubIssueSource` sends, against GitHub's schema.

The other tests answer queries from recordings, so a typo in a field, an
argument or an enum value would only fail on the live API. Offline here.
`fixtures/github-schema.graphql` is GitHub's public schema, refreshed with
`make schema` (`github_schema.py`).
"""

import json
from functools import cache
from pathlib import Path

import httpx
import pytest
from graphql import GraphQLSchema, build_schema, parse, validate

from cofabriq.issues import github
from cofabriq.issues.github import GitHubIssueSource

SCHEMA = Path(__file__).parent / "fixtures" / "github-schema.graphql"


@cache
def schema() -> GraphQLSchema:
    return build_schema(SCHEMA.read_text())


def errors(query: str) -> list[str]:
    return [e.message for e in validate(schema(), parse(query))]


@pytest.mark.parametrize(
    "name", ["QUERY", "ISSUE_QUERY", "SEARCH_QUERY", "COUNT_QUERY", "PULLS_QUERY"]
)
def test_query_is_valid(name: str) -> None:
    assert errors(getattr(github, name)) == []


def test_history_query_is_valid() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"data": {"repository": {}}})

    source = GitHubIssueSource("o/n", "t", transport=httpx.MockTransport(handler))
    source.history([12, 34])

    assert len(sent) == 1
    assert errors(sent[0]) == []


def test_a_typo_is_caught() -> None:
    query = github.QUERY.replace("stateReason", "stateReson")

    assert errors(query) == [
        "Cannot query field 'stateReson' on type 'Issue'."
        " Did you mean 'stateReason' or 'state'?"
    ]
