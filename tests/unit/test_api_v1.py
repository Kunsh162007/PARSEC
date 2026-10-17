"""One parent endpoint, /api/v1, with a child per due diligence category and the data room under it."""
import pytest
from fastapi.testclient import TestClient

import apps.api.main as main
from packages.review.guard import ReviewGuard
from tests.unit.test_api_security import AUTH, TOKEN, FakePipeline, FakeTavily

CONTRACT = (
    "The Supplier may terminate on a change of control of the Customer. Liability is unlimited. "
    "The Company owes $2,000,000 in back taxes after a tax audit. Insurance valid until 14 March 2023."
)
DOC = {"name": "contract.txt", "text": CONTRACT}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=TOKEN, per_minute=1000))
    monkeypatch.setattr(main, "DiligencePipeline", FakePipeline)
    monkeypatch.setattr("packages.tavily.client.TavilyRealityEngine", FakeTavily)
    main.deals_store.clear()
    yield TestClient(main.app)
    main.deals_store.clear()


def diligence(client, category, headers=None, **body):
    return client.post(f"/api/v1/diligence/{category}", json={"documents": [DOC], **body}, headers=headers or {})


# ---- the parent endpoint lists every child

def test_parent_endpoint_lists_children_that_really_exist(client):
    body = client.get("/api/v1").json()
    routes = {(m, r.path) for r in main.app.routes for m in getattr(r, "methods", ())}
    assert body["endpoints"]
    for e in body["endpoints"]:
        assert (e["method"], e["path"]) in routes, e
    paths = {e["path"] for e in body["endpoints"]}
    assert "/api/v1/diligence/{category}" in paths
    assert "/api/v1/data-room/run" in paths


def test_categories_child_lists_all_ten(client):
    cats = client.get("/api/v1/diligence/categories").json()["categories"]
    assert len(cats) == 10
    assert {"legal", "financial", "integrity"} <= {c["id"] for c in cats}
    assert all(c["path"] == f"/api/v1/diligence/{c['id']}" for c in cats)


# ---- one endpoint per category

def test_category_endpoint_runs_the_free_rules_without_an_access_code(client):
    res = diligence(client, "legal")
    assert res.status_code == 200
    body = res.json()
    assert body["category"]["id"] == "legal"
    assert body["findings"] and all(f["lens"] == "legal" for f in body["findings"])
    assert all(f["lens"] == "integrity" for f in body["integrity"])
    span = body["findings"][0]["spans"][0]
    assert CONTRACT[span["start"]:span["end"]] == span["text"]
    assert body["ai"]["requested"] is False


def test_each_category_returns_only_its_own_findings(client):
    tax = diligence(client, "tax").json()
    assert all(f["lens"] == "tax" for f in tax["findings"])
    assert not any(f["lens"] == "legal" for f in tax["findings"])


def test_integrity_category_returns_the_always_on_checks(client):
    body = diligence(client, "integrity", as_of="2026-01-01").json()
    assert any(f["id"] == "int-expired" for f in body["findings"])
    assert body["integrity"] == []


def test_unknown_category_is_404_and_names_the_valid_ones(client):
    res = diligence(client, "astrology")
    assert res.status_code == 404
    assert "legal" in res.json()["detail"]


def test_rules_endpoint_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=TOKEN, per_minute=1))
    assert diligence(client, "legal").status_code == 200
    assert diligence(client, "legal").status_code == 429


def test_oversized_input_is_rejected(client):
    res = client.post("/api/v1/diligence/legal", json={"documents": [{"name": "a", "text": "x" * 200_001}]})
    assert res.status_code == 422


def test_ai_read_needs_the_access_code_and_is_scoped_to_the_category(client, monkeypatch):
    seen = {}

    async def fake_deep(gateway, documents, lenses, use_triage=False, use_adjudication=False):
        seen["lenses"] = lenses
        return {"findings": [
            {"id": "ai-1", "source": "ai", "lens": "tax", "severity": "high", "title": "t", "why": "", "ask": "",
             "spans": [{"doc": 0, "start": 0, "end": 12}]},
            {"id": "ai-2", "source": "ai", "lens": "people", "severity": "high", "title": "p", "why": "", "ask": "",
             "spans": [{"doc": 0, "start": 0, "end": 12}]},
        ], "cost_usd": 0.1, "dropped": 0}

    async def no_clauses(gateway, documents):
        raise AssertionError("the clause pass is for the legal category only")

    monkeypatch.setattr(main, "deep_review", fake_deep)
    monkeypatch.setattr(main, "clause_findings", no_clauses)
    assert diligence(client, "tax", ai=True).status_code == 401
    body = diligence(client, "tax", headers=AUTH, ai=True).json()
    assert seen["lenses"] == ["tax"]
    ids = {f["id"] for f in body["findings"]}
    assert "ai-1" in ids and "ai-2" not in ids
    assert [f["id"] for f in body["other_categories"]] == ["ai-2"]   # paid for, so returned, but kept apart
    assert body["ai"]["ran"] is True
    assert body["ai"]["cost_usd"] == pytest.approx(0.1)
    ai = next(f for f in body["findings"] if f["id"] == "ai-1")
    assert ai["spans"][0]["text"] == CONTRACT[0:12]


def test_integrity_ai_read_asks_only_about_integrity(client, monkeypatch):
    seen = {}

    async def fake_deep(gateway, documents, lenses, use_triage=False, use_adjudication=False):
        seen["lenses"] = lenses
        return {"findings": [], "cost_usd": 0.0, "dropped": 0}

    monkeypatch.setattr(main, "deep_review", fake_deep)
    diligence(client, "integrity", headers=AUTH, ai=True)
    assert seen["lenses"] == ["integrity"]   # an empty list would mean "all areas", paid for and then hidden


def test_legal_ai_read_includes_the_clause_pass(client, monkeypatch):
    async def fake_deep(gateway, documents, lenses, use_triage=False, use_adjudication=False):
        return {"findings": [], "cost_usd": 0.0, "dropped": 0}

    async def fake_clauses(gateway, documents):
        return {"findings": [{"id": "clause-coc-1", "source": "ai", "lens": "legal", "severity": "high",
                              "title": "Change of control", "why": "", "ask": "",
                              "spans": [{"doc": 0, "start": 4, "end": 12}]}],
                "usage": {"cost_usd": 0.05}}

    monkeypatch.setattr(main, "deep_review", fake_deep)
    monkeypatch.setattr(main, "clause_findings", fake_clauses)
    body = diligence(client, "legal", headers=AUTH, ai=True).json()
    assert "clause-coc-1" in {f["id"] for f in body["findings"]}


def test_ai_unavailable_is_reported_and_rules_still_stand(client, monkeypatch):
    async def unavailable(gateway, documents, lenses, use_triage=False, use_adjudication=False):
        raise main.DeepReviewUnavailable("No Nebius API key is configured on the server.")

    monkeypatch.setattr(main, "deep_review", unavailable)
    res = diligence(client, "tax", headers=AUTH, ai=True)
    assert res.status_code == 200
    body = res.json()
    assert body["ai"]["ran"] is False
    assert "Nebius" in body["ai"]["error"]


def test_web_check_on_a_category_asks_that_categorys_questions(client, monkeypatch):
    seen = {}

    async def fake_web(documents, categories=None):
        seen["categories"] = categories
        return {"available": True, "reason": "", "checked": [], "lookups": [], "findings": []}

    monkeypatch.setattr(main, "web_check", fake_web)
    res = diligence(client, "tax", headers=AUTH, web=True)
    assert res.status_code == 200
    assert seen["categories"] == ["tax"]


def test_cross_category_deep_read_passes_its_lenses_to_the_web_check(client, monkeypatch):
    seen = {}

    async def fake_web(documents, categories=None):
        seen["categories"] = categories
        return {"available": True, "reason": "", "checked": [], "lookups": [], "findings": []}

    monkeypatch.setattr(main, "web_check", fake_web)
    client.post("/api/v1/diligence/deep", json={"documents": [DOC], "lenses": ["legal", "tech"], "ai": False,
                                                "web": True}, headers=AUTH)
    assert seen["categories"] == ["legal", "tech"]


def test_findings_say_which_documents_they_are_in_and_flag_cross_document_ones(client):
    docs = [{"name": "cim.txt", "text": "Revenue for FY2024 was $10.0 million."},
            {"name": "accounts.txt", "text": "Revenue for FY2024 was $9.1 million. Liability is unlimited."}]
    body = client.post("/api/v1/diligence", json={"documents": docs, "categories": ["legal", "integrity"]}).json()
    conflict = next(f for f in body["findings"] if f["id"].startswith("int-conflict"))
    assert conflict["cross_document"] is True
    assert sorted(conflict["documents"]) == ["accounts.txt", "cim.txt"]
    legal = next(f for f in body["findings"] if f["id"] == "leg-unlimited")
    assert legal["cross_document"] is False and legal["documents"] == ["accounts.txt"]


# ---- several categories in one call

def test_combined_diligence_runs_the_chosen_categories(client):
    res = client.post("/api/v1/diligence", json={"documents": [DOC], "categories": ["legal", "tax"]})
    assert res.status_code == 200
    body = res.json()
    assert body["categories"] == ["legal", "tax"]
    assert {f["lens"] for f in body["findings"]} <= {"legal", "tax"}


def test_combined_diligence_rejects_an_unknown_category(client):
    res = client.post("/api/v1/diligence", json={"documents": [DOC], "categories": ["legal", "astrology"]})
    assert res.status_code == 422


# ---- the data room lives under the same parent

def test_data_room_children_work_and_are_guarded(client):
    run = lambda h: client.post("/api/v1/data-room/run", json={"documents": {"cim.txt": "Adjusted EBITDA 10"}}, headers=h)
    assert run({}).status_code == 401
    deal = run(AUTH).json()
    deal_id = deal["deal_id"]
    assert client.get(f"/api/v1/data-room/{deal_id}").status_code == 401
    assert client.get(f"/api/v1/data-room/{deal_id}", headers=AUTH).status_code == 200
    signed = client.post(f"/api/v1/data-room/{deal_id}/findings/FINDING-1/sign", headers=AUTH)
    assert signed.json()["finding"]["signed_by_analyst"] is True


def test_requirements_memo_status_and_lookup_are_children_too(client):
    assert client.get("/api/v1/status").status_code == 200
    assert client.get("/api/v1/health").json()["status"] == "ok"
    assert client.get("/api/v1/companies/lookup", params={"company_name": "Acme"}, headers=AUTH).status_code == 200
    assert client.post("/api/v1/diligence/memo", json={}).status_code == 401
    assert client.post("/api/v1/diligence/requirements", json={"documents": [DOC]}).status_code == 401


def test_legacy_paths_still_answer(client):
    assert client.get("/api/review/status").status_code == 200
    assert client.post("/api/deals/run", json={"documents": {"a.txt": "x"}}, headers=AUTH).status_code == 200


# ---- one home page for everything

def test_home_page_is_the_list_of_deals_with_the_assistant(client):
    res = client.get("/")
    assert res.status_code == 200
    assert 'id="deal-list"' in res.text and "/review/" in res.text
    assert "/voice.js" in res.text and "/home.js" in res.text
    for asset in ("/voice.js", "/voice.css", "/home.js"):
        assert client.get(asset).status_code == 200, asset
    assert client.get("/review/").status_code == 200
    assert client.get("/parsec/").status_code == 200   # still served for specialists, no longer in the menu


def test_pages_do_not_talk_about_models_or_tokens(client):
    for path in ("/", "/review/"):
        text = client.get(path).text
        for word in ("Nemotron", "Nebius", "tokens", "API key"):
            assert word not in text, (path, word)


def test_pages_and_scripts_are_revalidated_so_an_update_is_never_served_stale(client):
    for path in ("/", "/review/", "/review/engine.js", "/parsec/app.js", "/nav.css"):
        assert client.get(path).headers.get("Cache-Control") == "no-cache", path


def test_home_page_links_every_category(client):
    html = client.get("/").text
    for category in main.LENS_IDS:
        assert f"/review/?category={category}" in html


def test_unknown_api_paths_get_a_json_404_not_the_home_page(client):
    for method in ("get", "post"):
        res = getattr(client, method)("/api/v1/nothing-here")
        assert res.status_code == 404
        assert "detail" in res.json()
