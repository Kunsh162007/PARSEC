import asyncio

from packages.review.web import extract_entities, web_check

DOCS = [
    {
        "name": "supply.txt",
        "text": (
            "This Agreement is made between Brightwell Components Ltd and Harlow Foods plc. "
            "Brightwell Components Ltd will deliver goods. The Supplier is paid monthly. "
            "Harlow Foods plc guarantees payment."
        ),
    }
]


def run(coro):
    return asyncio.run(coro)


def test_extract_entities_finds_company_names_by_frequency():
    names = extract_entities(DOCS)
    assert names[0] == "Brightwell Components Ltd"
    assert "Harlow Foods plc" in names
    assert "The Supplier" not in names


def test_without_a_key_the_check_reports_unavailable_and_never_invents_results():
    out = run(web_check(DOCS, api_key=""))
    assert out["available"] is False
    assert out["findings"] == []
    assert "Tavily" in out["reason"]


def test_mock_keys_are_treated_as_unavailable():
    assert run(web_check(DOCS, api_key="mock-tavily-key"))["available"] is False


def test_risky_result_about_the_entity_becomes_a_sourced_finding():
    async def search(query):
        return [
            {"title": "Brightwell Components Ltd fined over safety breaches",
             "url": "https://news.example/brightwell-fined", "content": "Regulator fined Brightwell Components Ltd..."},
            {"title": "Unrelated gardening tips", "url": "https://x.example/g", "content": "roses"},
        ]

    out = run(web_check(DOCS, search=search, max_entities=1))
    assert out["available"] is True
    f = out["findings"][0]
    assert f["source"] == "web"
    assert f["sources"][0]["url"] == "https://news.example/brightwell-fined"
    assert len(f["sources"]) == 1
    span = f["spans"][0]
    assert DOCS[0]["text"][span["start"]:span["end"]] == "Brightwell Components Ltd"


def test_results_about_a_different_company_are_ignored():
    async def search(query):
        return [{"title": "Acme Corp sued for fraud", "url": "https://n.example/a", "content": "fraud lawsuit"}]

    out = run(web_check(DOCS, search=search, max_entities=1))
    assert out["findings"] == []
    assert out["checked"] == ["Brightwell Components Ltd"]


def test_non_http_urls_are_dropped():
    async def search(query):
        return [{"title": "Brightwell Components Ltd lawsuit", "url": "javascript:alert(1)", "content": "Brightwell Components Ltd lawsuit"}]

    assert run(web_check(DOCS, search=search, max_entities=1))["findings"] == []


RISKY = {"title": "Brightwell Components Ltd fined over safety breaches",
         "url": "https://news.example/brightwell-fined", "content": "Regulator fined Brightwell Components Ltd."}
UNRELATED = {"title": "Unrelated gardening tips", "url": "https://x.example/g", "content": "roses"}


def test_each_lookup_is_reported_with_how_many_results_mattered():
    async def search(query):
        return [RISKY, UNRELATED]

    out = run(web_check(DOCS, search=search, max_entities=1))
    assert out["lookups"] == [{"entity": "Brightwell Components Ltd", "results": 2, "relevant": 1, "error": ""}]


def test_a_clean_company_is_reported_as_checked_not_as_missing():
    async def search(query):
        return [UNRELATED]

    out = run(web_check(DOCS, search=search, max_entities=1))
    assert out["findings"] == []
    assert out["lookups"][0]["results"] == 1 and out["lookups"][0]["relevant"] == 0


def test_partial_failure_is_reported_not_hidden():
    async def search(query):
        if "Harlow" in query:
            raise RuntimeError("boom")
        return []

    out = run(web_check(DOCS, search=search, max_entities=2, retries=0))
    assert out["available"] is True
    assert "1 of 2" in out["reason"]
    assert any(l["error"] for l in out["lookups"])


def test_a_transient_failure_is_retried_once():
    attempts = {"n": 0}

    async def search(query):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("429 too many requests")
        return [RISKY]

    out = run(web_check(DOCS, search=search, max_entities=1, retry_delay=0))
    assert attempts["n"] == 2
    assert len(out["findings"]) == 1
    assert out["lookups"][0]["error"] == ""


def test_search_errors_do_not_become_findings():
    async def search(query):
        raise RuntimeError("network down")

    out = run(web_check(DOCS, search=search, max_entities=1))
    assert out["findings"] == []
    assert out["available"] is False
    assert "failed" in out["reason"].lower()
