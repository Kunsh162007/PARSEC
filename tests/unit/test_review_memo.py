"""The memo is held to its evidence: uncited points and unsupported numbers are dropped and counted.

A fake gateway returns canned text; this checks the validation, not the quality of a real model's writing.
"""
import asyncio
import json
from types import SimpleNamespace

from packages.gateway.llm import GatewayUnavailable
from packages.review.memo import build_evidence, draft_memo, validate_memo

FINDINGS = [
    {"severity": "high", "title": "Uncapped liability", "where": "supply.txt",
     "quote": "Liability of the Supplier shall be unlimited for breaches of clause 12.", "detail": ""},
    {"severity": "high", "title": "Different figures given for revenue for 2023", "where": "cim.txt, accounts.txt",
     "quote": "Revenue was $12.4 million in 2023.", "detail": "12.4 million versus 11.9 million"},
]
NEEDS = [{"item": "Audited financial statements", "why": "Only management accounts were supplied."}]


class FakeGateway:
    def __init__(self, content=None, fail=False):
        self.content, self.fail = content, fail

    async def complete(self, task, messages, response_schema=None, use_cache=True):
        if self.fail:
            raise GatewayUnavailable("nvidia/ultra: HTTP 503")
        return SimpleNamespace(content=self.content, model_used="fake-ultra", usage=SimpleNamespace(cost_usd=0.02))


def memo(*points):
    return json.dumps({"sections": [{"heading": "What matters most", "points": list(points)}]})


def test_evidence_is_numbered_findings_then_needs():
    ev = build_evidence(FINDINGS, NEEDS)
    assert list(ev) == ["F1", "F2", "N1"]
    assert "unlimited" in ev["F1"]["text"] and "Audited" in ev["N1"]["text"]


def test_a_cited_point_whose_numbers_are_in_the_evidence_is_kept():
    ev = build_evidence(FINDINGS, NEEDS)
    out = validate_memo(memo({"text": "Revenue is stated as 12.4 million in one place and 11.9 million in another.", "cites": ["F2"]}), ev)
    assert out["sections"][0]["points"][0]["cites"] == ["F2"]
    assert out["dropped"] == {"uncited": 0, "unsupported_numbers": 0}


def test_a_point_with_an_invented_number_is_dropped():
    ev = build_evidence(FINDINGS, NEEDS)
    out = validate_memo(memo({"text": "Revenue fell 40% last year.", "cites": ["F2"]}), ev)
    assert out["sections"] == []
    assert out["dropped"]["unsupported_numbers"] == 1


def test_a_number_must_be_in_the_cited_evidence_not_just_anywhere():
    ev = build_evidence(FINDINGS, NEEDS)
    out = validate_memo(memo({"text": "Clause 12 carves breaches out of the cap.", "cites": ["F2"]}), ev)
    assert out["dropped"]["unsupported_numbers"] == 1


def test_points_without_a_real_citation_are_dropped():
    ev = build_evidence(FINDINGS, NEEDS)
    out = validate_memo(memo(
        {"text": "The company is well run.", "cites": []},
        {"text": "Ask for the board minutes.", "cites": ["F9"]},
        {"text": "Ask for audited accounts.", "cites": ["N1"]},
    ), ev)
    assert [p["text"] for p in out["sections"][0]["points"]] == ["Ask for audited accounts."]
    assert out["dropped"]["uncited"] == 2


def test_writing_an_evidence_id_in_the_text_is_not_treated_as_a_figure():
    ev = build_evidence(FINDINGS, NEEDS)
    out = validate_memo(memo({"text": "See F1: liability is unlimited.", "cites": ["F1"]}), ev)
    assert out["dropped"]["unsupported_numbers"] == 0


def test_end_to_end_memo_is_returned_with_model_and_cost():
    content = memo({"text": "Liability under clause 12 is unlimited.", "cites": ["F1"]})
    out = asyncio.run(draft_memo(FakeGateway(content), FINDINGS, NEEDS, "Acquisition review"))
    assert out["available"] is True and out["model"] == "fake-ultra" and out["cost_usd"] == 0.02
    assert out["sections"][0]["points"][0]["text"].startswith("Liability")


def test_no_model_means_no_memo_and_a_reason():
    out = asyncio.run(draft_memo(FakeGateway(fail=True), FINDINGS, NEEDS))
    assert out["available"] is False and "HTTP 503" in out["reason"] and out["sections"] == []


def test_a_memo_with_nothing_traceable_is_refused():
    out = asyncio.run(draft_memo(FakeGateway(memo({"text": "All good.", "cites": []})), FINDINGS, NEEDS))
    assert out["available"] is False and out["dropped"]["uncited"] == 1


def test_nothing_to_write_about_does_not_call_the_model():
    out = asyncio.run(draft_memo(FakeGateway(fail=True), [], []))
    assert out["available"] is False and "no findings" in out["reason"]


def test_number_words_must_also_be_in_the_evidence():
    ev = build_evidence([{"severity": "high", "title": "Lead time", "where": "c.txt",
                          "quote": "Distributor must send a Purchase Order at least six (6) months before shipment.", "detail": ""}], [])
    kept = validate_memo(memo({"text": "Orders need six months' notice.", "cites": ["F1"]}), ev)
    assert kept["dropped"]["unsupported_numbers"] == 0
    dropped = validate_memo(memo({"text": "Orders need twelve months' notice.", "cites": ["F1"]}), ev)
    assert dropped["dropped"]["unsupported_numbers"] == 1


def test_one_as_a_pronoun_is_not_treated_as_a_figure():
    ev = build_evidence(FINDINGS, NEEDS)
    out = validate_memo(memo({"text": "One of the parties has unlimited liability.", "cites": ["F1"]}), ev)
    assert out["dropped"]["unsupported_numbers"] == 0


def test_a_number_word_hidden_inside_another_word_does_not_back_it():
    ev = build_evidence([{"severity": "high", "title": "Claims", "where": "c.txt",
                          "quote": "The content of the claims schedule is incomplete.", "detail": ""}], [])
    out = validate_memo(memo({"text": "Ten claims are pending.", "cites": ["F1"]}), ev)
    assert out["dropped"]["unsupported_numbers"] == 1


def test_citing_f3_does_not_let_an_invented_3_through():
    findings = FINDINGS + [{"severity": "medium", "title": "Liability cap", "where": "msa.txt",
                            "quote": "Liability is capped at a multiple of the fees paid.", "detail": ""}]
    ev = build_evidence(findings, NEEDS)
    out = validate_memo(memo({"text": "Liability capped at 3 times fees.", "cites": ["F3"]}), ev)
    assert out["dropped"]["unsupported_numbers"] == 1


def test_inline_evidence_ids_in_brackets_or_lists_are_not_figures():
    ev = build_evidence(FINDINGS, NEEDS)
    out = validate_memo(memo({"text": "Liability is unlimited [F1] and accounts are unaudited (F1, N1).",
                              "cites": ["F1", "N1"]}), ev)
    assert out["dropped"]["unsupported_numbers"] == 0


def test_a_number_with_a_different_magnitude_than_the_evidence_is_dropped():
    ev = build_evidence([{"severity": "high", "title": "Cap", "where": "c.txt",
                          "quote": "Total liability is capped at 3 million.", "detail": ""}], [])
    assert validate_memo(memo({"text": "Cap of 3 billion.", "cites": ["F1"]}), ev)["dropped"]["unsupported_numbers"] == 1
    assert validate_memo(memo({"text": "Cap of 3 million.", "cites": ["F1"]}), ev)["dropped"]["unsupported_numbers"] == 0
    assert validate_memo(memo({"text": "Cap of 3%.", "cites": ["F1"]}), ev)["dropped"]["unsupported_numbers"] == 1
