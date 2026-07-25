"""Research skill: search -> cited synthesis -> Telegram push.

Regression coverage for a real production bug: ResearchSkill.search() called self._notify(...)
but __init__ never set it (only the unrelated DuckDuckGoSearcher class did), so every "research
X" / "find X" crashed with AttributeError the moment it had sources to deliver.
"""

from __future__ import annotations

from skills.research import ResearchSkill


class _FakeSearcher:
    def __init__(self, results: list[dict[str, str]]) -> None:
        self._results = results
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        self.queries.append(query)
        return self._results


def _skill(fake_llm, results, notified: list[str]) -> ResearchSkill:
    fake_llm.post_result = "Camaros are fast and iconic. [1]"
    return ResearchSkill(
        llm=fake_llm,
        searcher=_FakeSearcher(results),
        notify=lambda text: notified.append(text) or True,
    )


def test_search_with_sources_pushes_the_cited_answer_to_telegram(fake_llm):
    notified: list[str] = []
    skill = _skill(fake_llm, [{"title": "Camaro", "url": "https://x.test/camaro", "snippet": "..."}],
                    notified)

    result = skill.search("Camaro")

    assert result.ok
    assert len(notified) == 1
    assert "Camaro" in notified[0]
    assert "https://x.test/camaro" in notified[0]


def test_search_never_calls_notify_on_a_class_that_lacks_it(fake_llm):
    """The exact shape of the production crash: instantiate with NO notify override and
    confirm the instance actually has a callable _notify before search() ever needs it."""
    skill = ResearchSkill(llm=fake_llm, searcher=_FakeSearcher(
        [{"title": "Camaro", "url": "https://x.test/camaro", "snippet": "..."}]))
    assert callable(skill._notify)


def test_search_with_no_results_does_not_notify(fake_llm):
    notified: list[str] = []
    skill = _skill(fake_llm, [], notified)

    result = skill.search("something obscure")

    assert result.ok
    assert notified == []
    assert "couldn't find" in result.text.lower()


def test_search_with_an_empty_query_asks_what_to_look_up(fake_llm):
    notified: list[str] = []
    skill = _skill(fake_llm, [], notified)

    result = skill.search("")

    assert result.ok is False
    assert notified == []


def test_deliver_full_false_skips_the_telegram_push(fake_llm):
    notified: list[str] = []
    skill = _skill(fake_llm, [{"title": "Camaro", "url": "https://x.test/camaro", "snippet": "..."}],
                    notified)

    skill.search("Camaro", deliver_full=False)

    assert notified == []
