"""API-level checks: paid endpoints are guarded, inputs are bounded, deal ids are unguessable, headers are set."""
import logging

import pytest
from fastapi.testclient import TestClient

import apps.api.main as main
from packages.review.guard import ReviewGuard
from packages.tavily.client import RealityCheckResult

TOKEN = "access-code"
AUTH = {"X-Review-Token": TOKEN}
DOCS = {"cim.txt": "Adjusted EBITDA 10"}


class FakePipeline:
    """Stands in for DiligencePipeline: no network, a fixed paid cost."""

    runs = 0

    def __init__(self, sector: str = "saas"):
        self.sector = sector

    async def run_pipeline(self, deal):
        FakePipeline.runs += 1
        return {
            "deal_id": "DEAL-0123456789ab",
            "company_name": deal["company_name"],
            "findings": [{"id": "FINDING-1", "signed_by_analyst": False}],
            "cost_usd": 0.3,
        }


class FakeTavily:
    async def verify_external_claims(self, company, claims):
        return RealityCheckResult(query="q", available=False, reason="test")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=TOKEN, per_minute=1000))
    monkeypatch.setattr(main, "DiligencePipeline", FakePipeline)
    monkeypatch.setattr("packages.tavily.client.TavilyRealityEngine", FakeTavily)
    main.deals_store.clear()
    yield TestClient(main.app)
    main.deals_store.clear()


def run_deal(client, headers=AUTH, **body):
    return client.post("/api/deals/run", json={"documents": DOCS, **body}, headers=headers)


# ---- H1: paid endpoints are guarded and bounded

def test_deal_run_requires_the_access_code(client):
    assert run_deal(client, headers={}).status_code == 401
    assert run_deal(client).status_code == 200


def test_company_lookup_requires_the_access_code(client):
    assert client.get("/api/companies/lookup", params={"company_name": "Acme"}).status_code == 401
    res = client.get("/api/companies/lookup", params={"company_name": "Acme"}, headers=AUTH)
    assert res.status_code == 200
    assert res.json()["available"] is False


def test_deal_run_charges_the_pipeline_cost_to_the_daily_budget(client):
    before = main.review_guard.status()["budget_remaining_usd"]
    run_deal(client)
    assert main.review_guard.status()["budget_remaining_usd"] == pytest.approx(before - 0.3)


def test_deal_run_is_refused_once_the_budget_is_used(client, monkeypatch):
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=TOKEN, per_minute=1000, daily_budget_usd=0.2))
    assert run_deal(client).status_code == 200      # costs 0.3, over the 0.2 budget
    assert run_deal(client).status_code == 429


@pytest.mark.parametrize("body", [
    {"documents": {f"d{n}.txt": "x" for n in range(11)}},
    {"documents": {"n" * 201: "x"}},
    {"documents": {"a.txt": "x" * 200_001}},
    {"documents": DOCS, "company_name": "c" * 201},
])
def test_deal_run_rejects_oversized_input(client, body):
    runs_before = FakePipeline.runs
    assert client.post("/api/deals/run", json=body, headers=AUTH).status_code == 422
    assert FakePipeline.runs == runs_before   # rejected before any paid work


@pytest.mark.parametrize("name", ["", "c" * 201])
def test_company_lookup_bounds_the_name(client, name):
    assert client.get("/api/companies/lookup", params={"company_name": name}, headers=AUTH).status_code == 422


def test_deal_store_is_bounded_and_evicts_the_oldest(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_STORED_DEALS", 3)
    ids = [run_deal(client).json()["deal_id"] for _ in range(5)]
    assert list(main.deals_store) == ids[2:]


# ---- M1: deal ids are random and reading or signing a deal is guarded

def test_deal_ids_are_random_and_keep_the_document_hash_separately(client):
    a, b = run_deal(client).json(), run_deal(client).json()
    assert a["deal_id"] != b["deal_id"]
    assert a["document_hash"] == b["document_hash"] == "DEAL-0123456789ab"
    assert len(a["deal_id"]) >= 20


def test_reading_and_signing_a_deal_require_the_access_code(client):
    deal_id = run_deal(client).json()["deal_id"]
    assert client.get(f"/api/deals/{deal_id}").status_code == 401
    assert client.get(f"/api/deals/{deal_id}", headers=AUTH).status_code == 200
    sign = f"/api/deals/{deal_id}/findings/FINDING-1/sign"
    assert client.post(sign).status_code == 401
    res = client.post(sign, headers=AUTH)
    assert res.status_code == 200 and res.json()["finding"]["signed_by_analyst"] is True


# ---- deep review: a failing clause read is reported, not dropped

def test_deep_review_reports_a_clause_error_instead_of_dropping_it(client, monkeypatch):
    async def fake_deep(gateway, documents, lenses, use_triage=False, use_adjudication=False):
        return {"findings": [{"id": "ai-1"}], "cost_usd": 0.1}

    async def broken_clauses(gateway, documents):
        raise RuntimeError("clause reader exploded")

    monkeypatch.setattr(main, "deep_review", fake_deep)
    monkeypatch.setattr(main, "clause_findings", broken_clauses)
    before = main.review_guard.status()["budget_remaining_usd"]
    res = client.post("/api/review/deep", json={"documents": [{"name": "a.txt", "text": "hello"}]}, headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert "clause reader exploded" in body["clause_error"]
    assert body["findings"] == [{"id": "ai-1"}]
    assert main.review_guard.status()["budget_remaining_usd"] == pytest.approx(before - 0.1)


# ---- L1: security headers

@pytest.mark.parametrize("path", ["/health", "/review/", "/parsec/"])
def test_security_headers_are_set(client, path):
    res = client.get(path)
    assert res.status_code == 200
    csp = res.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "https://cdnjs.cloudflare.com" in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"


# ---- L2: an open server on a public interface says so at startup

@pytest.mark.parametrize("argv, expected", [
    (["uvicorn", "apps.api.main:app", "--host", "0.0.0.0"], "0.0.0.0"),
    (["uvicorn", "apps.api.main:app", "--host=192.168.1.5"], "192.168.1.5"),
    (["uvicorn", "apps.api.main:app"], "127.0.0.1"),
])
def test_bind_host_is_read_from_the_command_line(argv, expected):
    assert main._bind_host(argv, {}) == expected


def test_bind_host_falls_back_to_the_uvicorn_environment_variable():
    assert main._bind_host(["uvicorn"], {"UVICORN_HOST": "0.0.0.0"}) == "0.0.0.0"


@pytest.mark.parametrize("host, auth_required, warns", [
    ("0.0.0.0", False, True),
    ("192.168.1.5", False, True),
    ("::", False, True),
    ("127.0.0.1", False, False),
    ("localhost", False, False),
    ("::1", False, False),
    ("0.0.0.0", True, False),
])
def test_open_access_warning(host, auth_required, warns):
    message = main._open_access_warning(host, auth_required)
    assert bool(message) is warns
    if warns:
        assert "REVIEW_API_TOKEN" in message and "paid endpoints" in message


@pytest.mark.parametrize("host, token, warns", [("0.0.0.0", "", True), ("127.0.0.1", "", False), ("0.0.0.0", "x", False)])
def test_startup_warns_only_when_open_on_a_public_host(monkeypatch, caplog, host, token, warns):
    monkeypatch.setattr(main.sys, "argv", ["uvicorn", "apps.api.main:app", "--host", host])
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=token))
    with caplog.at_level(logging.WARNING, logger="parsec.api"):
        with TestClient(main.app):
            pass
    assert any("REVIEW_API_TOKEN" in r.getMessage() for r in caplog.records) is warns
