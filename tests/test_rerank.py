from collections.abc import Sequence
from pathlib import Path

from cofabriq.issues import rerank
from cofabriq.issues.model import Issue
from cofabriq.issues.rerank import Job, Order, Seen
from cofabriq.llm import Usage
from tests.builders import issue

USAGE = Usage(input_tokens=1000, output_tokens=50, latency_ms=900)


class FakeReranker:
    """Answers with fixed numbers and counts its calls."""

    def __init__(self, numbers: list[int]) -> None:
        self.numbers = numbers
        self.calls = 0

    def rerank(self, query: Issue, candidates: Sequence[Seen]) -> tuple[Order, Usage]:
        self.calls += 1
        return Order(numbers=self.numbers), USAGE


def job(query: int = 9, offered: Sequence[int] = (1, 2, 3, 4)) -> Job:
    return Job(issue(query), [Seen.of(issue(n, title=f"t{n}")) for n in offered])


def test_the_models_picks_come_first_and_the_rest_keep_vector_order() -> None:
    assert rerank.merge([1, 2, 3, 4], [3, 1]) == [3, 1, 2, 4]


def test_numbers_not_offered_and_repeats_are_dropped() -> None:
    assert rerank.merge([1, 2, 3], [99, 2, 2, 7, 3]) == [2, 3, 1]


def test_an_empty_answer_leaves_the_vector_order() -> None:
    assert rerank.merge([5, 4, 3], []) == [5, 4, 3]


def test_a_candidate_is_its_title_and_the_start_of_the_cleaned_body() -> None:
    body = "<!-- template -->\n" + "word " * 200
    seen = Seen.of(issue(1, title="Crash", body=body))

    assert seen.title == "Crash"
    assert seen.snippet == ("word " * 200).strip()[: rerank.SNIPPET]


def test_the_prompt_has_the_query_and_every_candidate_but_no_comments() -> None:
    commented = issue(2, title="t2", comments=[("duplicate of #9", False)])
    text = rerank.prompt(issue(9, title="Freeze"), [Seen.of(commented)])

    assert '<issue number="9">' in text and "Freeze" in text
    assert '<candidate number="2">' in text
    assert "duplicate of" not in text


def test_a_record_holds_the_full_order_and_the_cost(tmp_path: Path) -> None:
    fake = FakeReranker([3])

    done, _ = rerank.rerank_all([job()], tmp_path / "r.json", fake)

    assert done[9].order == [3, 1, 2, 4]
    assert done[9].candidates == [1, 2, 3, 4]
    assert done[9].cost_usd > 0


def test_the_answer_is_cut_to_the_short_list(tmp_path: Path) -> None:
    offered = list(range(1, 31))
    fake = FakeReranker(list(reversed(offered)))

    done, _ = rerank.rerank_all([job(offered=offered)], tmp_path / "r.json", fake)

    assert done[9].order[: rerank.KEEP] == list(range(30, 20, -1))
    assert done[9].order[rerank.KEEP :] == list(range(1, 21))


def test_a_record_is_reused_until_a_re_embed_changes_the_candidates(
    tmp_path: Path,
) -> None:
    path, fake = tmp_path / "r.json", FakeReranker([2])

    rerank.rerank_all([job(offered=(1, 2, 3))], path, fake)
    rerank.rerank_all([job(offered=(1, 2, 3))], path, fake)
    rerank.rerank_all([job(offered=(1, 2, 4))], path, fake)

    assert fake.calls == 2
