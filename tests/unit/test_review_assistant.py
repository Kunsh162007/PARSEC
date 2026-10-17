"""Nemotron as the assistant's fallback: free wording in, one checked command (or a cited answer) out."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import apps.api.main as main
from packages.gateway.llm import GatewayResponse, GatewayUnavailable, TokenUsage
from packages.review.assistant import interpret
from packages.review.guard import ReviewGuard

CONTEXT = {
    "deals": ["Acme Cloud", "Beta Foods"],
    "documents": ["1_cim_acme.txt", "3_customer_arr.txt"],
    "categories": ["financial", "legal", "tax", "commercial"],
    "findings": [
        {"n": 1, "title": "Disallowed add-back: Litigation Legal Fees", "severity": "high",
         "documents": ["1_cim_acme.txt"], "why": "The ledger shows it every year."},
        {"n": 2, "title": "Customer concentration: Northwind is 42%", "severity": "critical",
         "documents": ["3_customer_arr.txt"], "why": "One customer is 42% of revenue."},
    ],
}


class FakeGateway:
    is_configured = True

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.messages = reply, error, None

    async def complete(self, task, messages, use_cache=True, response_schema=None):
        self.messages = messages
        if self.error:
            raise self.error
        content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return GatewayResponse(content=content, parsed_json=None, model_used="nano", tier="nano",
                               usage=TokenUsage(cost_usd=0.0004))


def ask(reply=None, error=None, text="take me to whatever is wrong with the ARR file"):
    gateway = FakeGateway(reply, error)
    return asyncio.run(interpret(gateway, text, CONTEXT)), gateway


def test_free_wording_becomes_a_known_command_with_real_names():
    out, gateway = ask({"action": "problems", "document": "3_customer_arr.txt"})
    assert out["action"] == "problems" and out["document"] == "3_customer_arr.txt"
    assert out["cost_usd"] == pytest.approx(0.0004)
    prompt = json.dumps(gateway.messages)
    assert "3_customer_arr.txt" in prompt and "Acme Cloud" in prompt   # it chooses from what exists


def test_an_invented_document_or_folder_is_dropped():
    out, _ = ask({"action": "problems", "document": "secret_board_minutes.pdf", "deal": "Gamma"})
    assert out["action"] == "problems"
    assert "document" not in out and "deal" not in out


def test_opening_a_folder_that_does_not_exist_is_refused():
    out, _ = ask({"action": "open-deal", "deal": "Gamma"})
    assert out["action"] == "unknown"


def test_an_action_outside_the_list_is_refused():
    out, _ = ask({"action": "delete-all-files"})
    assert out["action"] == "unknown"


def test_a_question_is_answered_only_with_real_citations():
    out, _ = ask({"action": "answer", "answer": "Northwind is 42% of revenue, so losing it would hurt.", "cites": [2, 9]},
                 text="which customer worries you most")
    assert out["action"] == "answer" and out["cites"] == [2]
    out, _ = ask({"action": "answer", "answer": "The CEO was arrested last year.", "cites": []})
    assert out["action"] == "unknown"   # an answer that rests on no finding is not shown


def test_internal_problem_numbers_are_taken_out_of_the_answer():
    out, _ = ask({"action": "answer", "cites": [1, 2],
                  "answer": "Problems 1 and 2 cost the most; the legal fees (problem 1) alone are $9m."})
    assert "1" not in out["answer"].replace("$9m", "")
    assert out["answer"].startswith("These problems cost the most")


def test_cites_that_are_not_a_list_are_refused_not_a_crash():
    out, _ = ask({"action": "answer", "answer": "x", "cites": "1"})
    assert out["action"] == "unknown"


def test_document_text_in_the_prompt_is_fenced_off_as_data():
    _, gateway = ask({"action": "summary"})
    prompt = gateway.messages[1]["content"]
    assert "<findings>" in prompt and "</findings>" in prompt
    assert "data" in gateway.messages[0]["content"].lower()


def test_reasoning_text_around_the_json_is_tolerated():
    out, _ = ask('<think>the user wants the cim</think>\n```json\n{"action": "problems", "document": "1_cim_acme.txt"}\n```')
    assert out["document"] == "1_cim_acme.txt"


def test_no_model_or_a_bad_reply_is_reported_not_guessed():
    out, _ = ask(error=GatewayUnavailable("no key"))
    assert out["action"] == "unknown" and "no key" in out["reason"]
    out, _ = ask("I think you want the CIM")
    assert out["action"] == "unknown"


# ---- the endpoint

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token="code", per_minute=1000))
    return TestClient(main.app)


def test_endpoint_is_paid_so_it_needs_the_access_code_and_records_the_cost(client, monkeypatch):
    async def fake(gateway, text, context):
        return {"action": "summary", "cost_usd": 0.0004}

    monkeypatch.setattr(main, "interpret_command", fake)
    body = {"text": "how bad is this deal", **CONTEXT}
    assert client.post("/api/v1/assistant/interpret", json=body).status_code == 401
    before = main.review_guard.status()["budget_remaining_usd"]
    res = client.post("/api/v1/assistant/interpret", json=body, headers={"X-Review-Token": "code"})
    assert res.status_code == 200 and res.json()["action"] == "summary"
    assert main.review_guard.status()["budget_remaining_usd"] == pytest.approx(before - 0.0004)


def test_endpoint_bounds_its_input(client):
    res = client.post("/api/v1/assistant/interpret", json={"text": "x" * 501}, headers={"X-Review-Token": "code"})
    assert res.status_code == 422
    res = client.post("/api/v1/assistant/interpret", json={"text": "hi", "deals": ["x" * 401]},
                      headers={"X-Review-Token": "code"})
    assert res.status_code == 422


def test_requests_for_another_host_name_are_refused(client):
    # A web page that rebinds its own domain name to 127.0.0.1 still sends its own name as Host.
    res = client.get("/api/v1/deals", headers={"Host": "evil.example", "X-Review-Token": "code"})
    assert res.status_code == 400
