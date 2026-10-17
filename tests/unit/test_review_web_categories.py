"""The web check asks the internet different questions for each due diligence category."""
import asyncio

from packages.review import web
from packages.review.web import CATEGORY_TOPICS, MAX_WEB_QUERIES, web_check
from tests.unit.test_review_web import DOCS

NAME = "Brightwell Components Ltd"


def run(coro):
    return asyncio.run(coro)


def recorder(results):
    """A fake search that records every query and answers each with `results`."""
    queries = []

    async def search(query):
        queries.append(query)
        return results

    return search, queries


def test_every_category_except_integrity_has_its_own_search():
    assert set(CATEGORY_TOPICS) == {"financial", "legal", "compliance", "tech", "people", "property", "tax",
                                    "commercial", "supply"}
    queries = {t.query for t in CATEGORY_TOPICS.values()}
    assert len(queries) == len(CATEGORY_TOPICS)


def test_a_tax_check_searches_for_tax_matters_and_asks_a_tax_question():
    search, queries = recorder([{"title": f"{NAME} faces HMRC tax dispute", "url": "https://n.example/t",
                                 "content": f"{NAME} is in a tax dispute over back taxes."}])
    out = run(web_check(DOCS, search=search, max_entities=1, categories=["tax"]))
    assert len(queries) == 1 and "tax" in queries[0].lower() and NAME in queries[0]
    f = out["findings"][0]
    assert f["lens"] == "tax"
    assert "tax" in f["ask"].lower()
    assert out["lookups"][0]["category"] == "tax"


def test_relevance_uses_the_category_terms():
    search, _ = recorder([{"title": f"{NAME} tax audit", "url": "https://n.example/t",
                           "content": f"{NAME} under tax audit."}])
    out = run(web_check(DOCS, search=search, max_entities=1, categories=["tech"]))
    assert out["findings"] == []   # a tax story is not a technology finding


def test_several_categories_each_get_a_query_and_their_own_finding():
    search, queries = recorder([{"title": f"{NAME} data breach and lawsuit", "url": "https://n.example/x",
                                 "content": f"{NAME} hit by a data breach; customers file a lawsuit."}])
    out = run(web_check(DOCS, search=search, max_entities=1, categories=["tech", "legal"]))
    assert len(queries) == 2
    assert {f["lens"] for f in out["findings"]} == {"tech", "legal"}
    assert len({f["id"] for f in out["findings"]}) == 2


def test_queries_are_capped_and_the_cap_is_reported():
    search, queries = recorder([])
    out = run(web_check(DOCS, search=search, max_entities=3, categories=list(CATEGORY_TOPICS)))
    assert len(queries) == MAX_WEB_QUERIES
    assert "not searched" in out["reason"]


def test_integrity_only_or_no_categories_keeps_the_general_search():
    for categories in (None, [], ["integrity"]):
        search, queries = recorder([{"title": f"{NAME} fined", "url": "https://n.example/f",
                                     "content": f"Regulator fined {NAME}."}])
        out = run(web_check(DOCS, search=search, max_entities=1, categories=categories))
        assert queries == [f'"{NAME}" {web.QUERY_SUFFIX}']
        assert out["findings"][0]["lens"] == "compliance"
