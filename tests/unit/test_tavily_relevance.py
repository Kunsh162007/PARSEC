import asyncio

from packages.tavily.client import TavilyRealityEngine, TavilyUnavailable


def run(coro):
    return asyncio.run(coro)


def engine_returning(results):
    engine = TavilyRealityEngine(api_key="tvly-test")

    async def fake_search(query, search_depth="basic"):
        return {"results": results}

    engine.search = fake_search
    return engine


OTHER_COMPANY = {"title": "Titan Pharmaceuticals faces securities lawsuit", "url": "https://n.example/a",
                 "content": "Titan Pharmaceuticals shareholders filed a class action lawsuit."}
THIS_COMPANY = {"title": "Titan Cloud Inc sued over data practices", "url": "https://n.example/b",
                "content": "A lawsuit names Titan Cloud Inc as defendant."}


def test_results_about_other_companies_do_not_become_flags():
    result = run(engine_returning([OTHER_COMPANY]).verify_external_claims("Titan Cloud Inc", []))
    assert result.available is True
    assert result.flags == []
    assert result.claims_verified is True
    assert result.sources == []


def test_results_about_the_target_are_flagged_with_their_url():
    result = run(engine_returning([OTHER_COMPANY, THIS_COMPANY]).verify_external_claims("Titan Cloud Inc", []))
    assert len(result.flags) == 1
    assert "Titan Cloud Inc" in result.flags[0]
    assert result.sources[0]["url"] == "https://n.example/b"
    assert result.claims_verified is False


def test_without_a_key_the_check_is_unavailable_and_returns_nothing_invented(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    engine = TavilyRealityEngine(api_key="")
    result = run(engine.verify_external_claims("Anyone Ltd", []))
    assert result.available is False
    assert result.claims_verified is None
    assert result.flags == [] and result.sources == []
    assert "TAVILY_API_KEY" in result.reason


def test_a_failed_request_is_reported_not_turned_into_results():
    engine = TavilyRealityEngine(api_key="tvly-test")

    async def failing(query, search_depth="basic"):
        raise TavilyUnavailable("Tavily returned HTTP 429")

    engine.search = failing
    result = run(engine.verify_external_claims("Anyone Ltd", []))
    assert result.available is False
    assert "429" in result.reason
    assert result.claims_verified is None
