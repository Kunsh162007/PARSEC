"""Behaviour of the requirements step: parsing, quote verification, and honest failure.

The gateway is replaced by a fake that returns canned text. These tests check that replies are read and verified
correctly. They say nothing about how good a real model's judgement is.
"""
import asyncio
import json
from types import SimpleNamespace

from packages.gateway.llm import GatewayUnavailable
from packages.review.requirements import build_digest, clean_items, determine_requirements

DOCS = [
    {"name": "cim.txt", "text": "CONFIDENTIAL INFORMATION MEMORANDUM\nProject Falcon\nAdjusted EBITDA was $4.2 million.\n"},
    {"name": "ledger.txt", "text": "General ledger extract FY2023\nRevenue 10,000,000\n"},
]


class FakeGateway:
    def __init__(self, content=None, unavailable=False):
        self.content = content
        self.unavailable = unavailable
        self.calls = []

    async def complete(self, task, messages, response_schema=None, use_cache=True):
        self.calls.append({"task": task, "messages": messages})
        if self.unavailable:
            raise GatewayUnavailable("NEBIUS_API_KEY is not set.")
        return SimpleNamespace(content=self.content, model_used="fake-super", usage=SimpleNamespace(cost_usd=0.01))


def run(coro):
    return asyncio.run(coro)


def reply(items, engagement="Acquisition of a software company"):
    return json.dumps({"engagement": engagement, "items": items})


def test_digest_contains_names_openings_and_headings():
    digest = build_digest(DOCS)
    assert "cim.txt" in digest and "ledger.txt" in digest
    assert "CONFIDENTIAL INFORMATION MEMORANDUM" in digest
    assert "Headings:" in digest


def test_unavailable_model_returns_no_list_and_no_default():
    out = run(determine_requirements(FakeGateway(unavailable=True), DOCS))
    assert out["available"] is False
    assert out["items"] == []
    assert "NEBIUS_API_KEY" in out["reason"]


def test_unreadable_reply_is_reported_not_guessed():
    out = run(determine_requirements(FakeGateway(content="I think you need some documents."), DOCS))
    assert out["available"] is False
    assert out["items"] == []


def test_items_are_returned_with_the_models_terms_and_descriptions():
    content = reply([
        {"name": "Quality of earnings report", "name_known": True, "what_it_is": "", "why_needed": "Adjusted EBITDA is claimed.",
         "status": "missing", "evidence": "", "priority": "high"},
        {"name": "Revenue by customer, three years", "name_known": False,
         "what_it_is": "A table of revenue per customer for each of the last three years.", "why_needed": "Concentration cannot be tested.",
         "status": "missing", "evidence": "", "priority": "medium"},
    ])
    out = run(determine_requirements(FakeGateway(content=content), DOCS))
    assert out["available"] is True
    assert out["engagement"].startswith("Acquisition")
    assert [i["name"] for i in out["items"]] == ["Quality of earnings report", "Revenue by customer, three years"]
    assert out["items"][1]["name_known"] is False
    assert "per customer" in out["items"][1]["what_it_is"]
    assert out["cost_usd"] == 0.01


def test_supplied_claim_with_a_real_quote_is_kept():
    items = clean_items([{"name": "General ledger", "status": "supplied", "evidence": "General ledger extract FY2023"}], DOCS)
    assert items[0]["status"] == "supplied"
    assert items[0]["evidence"] == "General ledger extract FY2023"


def test_supplied_claim_with_a_quote_not_in_the_documents_is_downgraded():
    items = clean_items([{"name": "Audited accounts", "status": "supplied", "evidence": "Independent auditor's report to the members"}], DOCS)
    assert items[0]["status"] == "unclear"
    assert items[0]["evidence"] == ""
    assert "quote could not be found" in items[0]["note"]


def test_supplied_claim_without_any_quote_is_downgraded():
    items = clean_items([{"name": "Audited accounts", "status": "supplied", "evidence": ""}], DOCS)
    assert items[0]["status"] == "unclear"


def test_malformed_items_are_dropped_and_unknown_values_normalised():
    items = clean_items([
        "not an object",
        {"name": ""},
        {"name": "Board minutes", "status": "perhaps", "priority": "urgent"},
    ], DOCS)
    assert len(items) == 1
    assert items[0]["status"] == "unclear"
    assert items[0]["priority"] == "medium"


def test_no_documents_is_reported_without_calling_the_model():
    gateway = FakeGateway(content="{}")
    out = run(determine_requirements(gateway, []))
    assert out["available"] is False
    assert gateway.calls == []


def test_a_reply_cut_off_mid_list_keeps_the_complete_items():
    truncated = (
        '{"engagement": "Acquisition of a software company", "items": ['
        '{"name": "Quality of earnings report", "name_known": true, "what_it_is": "", "why_needed": "x", '
        '"status": "missing", "evidence": "", "priority": "high"}, '
        '{"name": "Customer contracts", "name_known": true, "what_it_is": "", "why_needed": "y", '
        '"status": "missing", "evidence": "", "priority": "medium"}, '
        '{"name": "Org chart", "name_known": tr'
    )
    out = run(determine_requirements(FakeGateway(content=truncated), DOCS))
    assert out["available"] is True
    assert [i["name"] for i in out["items"]] == ["Quality of earnings report", "Customer contracts"]
    assert out["engagement"].startswith("Acquisition")


def test_headings_come_from_the_whole_document_and_keep_structural_ones():
    from packages.review.requirements import headings_of

    body = "\n".join(f"Topic {n}" for n in range(200))
    text = f"FORM 10-K\n{body}\nItem 7. Management's Discussion and Analysis\nItem 8. Financial Statements\n"
    found = headings_of(text)
    assert "Item 7. Management's Discussion and Analysis" in found and "Item 8. Financial Statements" in found
    assert len(found) <= 60
    assert "FORM 10-K" in found and "Topic 199" in found  # first and last: the whole document is represented


def test_an_item_called_missing_that_is_a_section_of_a_supplied_document_becomes_unclear():
    docs = [{"name": "10-K.txt", "text": "FORM 10-K\nItem 7. Management's Discussion and Analysis\nItem 8. Consolidated Financial Statements\n"}]
    items = clean_items([
        {"name": "Consolidated Financial Statements", "status": "missing"},
        {"name": "Credit facility agreement", "status": "missing"},
    ], docs)
    assert items[0]["status"] == "unclear" and "Item 8. Consolidated Financial Statements" in items[0]["note"]
    assert items[1]["status"] == "missing"


def test_reference_vocabulary_is_not_sent_to_the_model():
    """config/dd_vocabulary.yaml anchored the model on a generic M&A list in a live run, so it stays out of the prompt."""
    gateway = FakeGateway(content=reply([]))
    out = run(determine_requirements(gateway, DOCS))
    system = gateway.calls[0]["messages"][0]["content"]
    assert "Reference vocabulary" not in system
    assert "Quality of earnings report (QoE)" not in system and "EBITDA bridge" not in system
    assert "vocabulary" not in out
    assert out["items"] == []


def test_the_model_is_not_asked_for_a_workstream():
    """Workstreams are assigned in code, so the prompt carries neither the field nor the list of ids."""
    from packages.review.requirements import PROMPT, WORKSTREAMS

    assert "workstream" not in PROMPT
    assert not any(w in PROMPT for w in ("it_security", "esg_environmental", "real_estate"))
    assert len(WORKSTREAMS) == 10


# Names taken from live runs on the sample sets, with the label a diligence team would file them under.
LABELLED = [
    ("Quality of earnings report (QoE)", "financial"),
    ("Audited financial statements of the franchisor", "financial"),
    ("Most recent Form 10-Q (quarterly report)", "financial"),
    ("Proxy Statement (DEF 14A) for the most recent annual meeting", "financial"),
    ("Internal control over financial reporting (SOX 404) documentation", "financial"),
    ("Board Resolution authorizing the Endorsement Agreement", "legal"),
    ("Certificate of Incorporation and Bylaws of Adams Golf", "legal"),
    ("Insurance certificates (general liability, property)", "legal"),
    ("Exhibit A - Scope of Services", "legal"),
    ("Federal and state tax returns of Adams Golf (past 3 years)", "tax"),
    ("Trademark registration certificates for Adams Golf product lines", "ip"),
    ("Joint Content License Agreement", "ip"),
    ("Real estate lease agreements for typical franchise locations", "real_estate"),
    ("SOC 2 Type II report", "it_security"),
    ("Employee headcount and compensation summary", "people"),
    ("Correspondence with SEC regarding the proceeding", "regulatory"),
    ("ESG/sustainability report", "esg_environmental"),
    ("Franchisee roster and performance report", "commercial"),
    ("Internal investigation report", "other"),
    ("Subsidiary organization chart", "legal"),
    ("Capitalization table", "legal"),
    ("Org chart", "people"),
    ("Financial Impact Analysis", "financial"),
]


def test_a_bracketed_part_of_a_vocabulary_name_is_not_split_into_stray_words():
    from packages.review.requirements import VOCABULARY_PATH, _lexicon

    alternatives = {tokens for _, tokens in _lexicon(str(VOCABULARY_PATH))}
    assert frozenset({"analysi"}) not in alternatives and frozenset({"analysis"}) not in alternatives
    assert frozenset({"fto"}) in alternatives


def test_workstream_labels_for_realistic_item_names():
    from packages.review.requirements import workstream_of

    got = [(name, workstream_of(name)) for name, _ in LABELLED]
    assert got == LABELLED


def test_description_is_used_when_the_name_says_nothing():
    from packages.review.requirements import workstream_of

    assert workstream_of("Item 4 materials", "Copies of the company's patent filings and assignments") == "ip"


def test_labeller_falls_back_to_keyword_rules_without_the_vocabulary_file(tmp_path):
    from packages.review.requirements import workstream_of

    absent = tmp_path / "absent.yaml"
    assert workstream_of("Payroll tax filings", vocabulary_path=absent) == "tax"
    assert workstream_of("Board minutes", vocabulary_path=absent) == "legal"
    # A vocabulary-only name loses its label without the file, rather than guessing.
    assert workstream_of("NWC peg") == "financial"
    assert workstream_of("NWC peg", vocabulary_path=absent) == "other"


def test_clean_items_labels_each_item_in_code_and_ignores_a_model_supplied_workstream():
    items = clean_items([
        {"name": "Quality of earnings report", "workstream": "legal"},
        {"name": "Something odd"},
    ], DOCS)
    assert [i["workstream"] for i in items] == ["financial", "other"]


class ScriptedGateway:
    """Returns one canned reply per call, in order; an exception in the script is raised instead."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, task, messages, response_schema=None, use_cache=True):
        self.calls.append({"task": task, "use_cache": use_cache})
        answer = self.replies[len(self.calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(content=answer, model_used="fake-super", usage=SimpleNamespace(cost_usd=0.01))


def item(name, status="missing", priority="medium", evidence=""):
    return {"name": name, "name_known": True, "what_it_is": "", "why_needed": "", "status": status,
            "evidence": evidence, "priority": priority}


def test_item_names_match_on_content_words_not_on_shared_modifiers():
    from packages.review.requirements import names_match

    assert names_match("Audited financial statements (FY2023-2025)", "Audited financial statements")
    assert names_match("Tax returns", "Federal and state tax returns")
    assert names_match("Board Resolutions Authorizing Agreement", "Board Resolutions Authorizing the Agreement")
    assert names_match("Form 10-K (Annual Report)", "Form 10‑K")
    assert not names_match("Audited financial statements", "Restated financial statements")
    assert not names_match("Financial statements", "Restated financial statements")
    assert not names_match("Form 10-K", "Form 10-Q")
    assert not names_match("Insurance policies", "Insurance certificates")


def test_consensus_keeps_only_items_a_majority_of_runs_list():
    runs = [
        reply([item("Quality of earnings report", priority="high"), item("Customer contracts"), item("Odd one")]),
        reply([item("Quality of earnings report (QoE)", priority="high"), item("Tax returns")]),
        reply([item("Quality of Earnings Report", priority="medium"), item("Material customer contracts"),
               item("Federal and state tax returns")]),
    ]
    gateway = ScriptedGateway(runs)
    out = run(determine_requirements(gateway, DOCS, samples=3))
    assert len(gateway.calls) == 3 and all(c["use_cache"] is False for c in gateway.calls)
    names = [i["name"] for i in out["items"]]
    assert names == ["Quality of earnings report", "Customer contracts", "Tax returns"]
    assert [(i["agreement"], i["of_runs"]) for i in out["items"]] == [(3, 3), (2, 3), (2, 3)]
    assert out["items"][0]["priority"] == "high"  # two of three runs said high
    assert out["samples"] == 3 and out["samples_ok"] == 3
    assert abs(out["cost_usd"] - 0.03) < 1e-9


def test_consensus_uses_the_runs_that_answered_and_says_so():
    runs = [GatewayUnavailable("timeout"), reply([item("Board minutes"), item("Cap table")]),
            reply([item("Board minutes")])]
    out = run(determine_requirements(ScriptedGateway(runs), DOCS, samples=3))
    assert out["available"] is True
    assert out["samples_ok"] == 2 and "2 of 3" in out["reason"]
    # of two runs, a majority is both
    assert [(i["name"], i["agreement"], i["of_runs"]) for i in out["items"]] == [("Board minutes", 2, 2)]


def test_consensus_with_every_run_failing_is_unavailable():
    runs = [GatewayUnavailable("NEBIUS_API_KEY is not set."), "not json", GatewayUnavailable("down")]
    out = run(determine_requirements(ScriptedGateway(runs), DOCS, samples=3))
    assert out["available"] is False and out["items"] == []
    assert out["samples_ok"] == 0


def test_consensus_status_is_the_majority_and_a_tie_is_unclear():
    from packages.review.requirements import merge_runs

    quoted = item("EBITDA bridge", status="supplied", evidence="Adjusted EBITDA was $4.2 million")
    majority = merge_runs([
        clean_items([quoted], DOCS), clean_items([quoted], DOCS), clean_items([item("EBITDA bridge")], DOCS)])
    assert majority[0]["status"] == "supplied" and majority[0]["evidence"] == "Adjusted EBITDA was $4.2 million"

    tie = merge_runs([clean_items([quoted], DOCS), clean_items([item("EBITDA bridge")], DOCS)])
    assert tie[0]["status"] == "unclear" and tie[0]["evidence"] == ""
    assert "disagreed" in tie[0]["note"]


def test_consensus_supplied_cannot_win_on_an_unverified_quote():
    from packages.review.requirements import merge_runs

    fake = item("Audited accounts", status="supplied", evidence="Independent auditor's report to the members")
    merged = merge_runs([clean_items([fake], DOCS), clean_items([fake], DOCS), clean_items([fake], DOCS)])
    assert merged[0]["status"] == "unclear"


def test_consensus_priority_tie_takes_the_higher_priority():
    from packages.review.requirements import merge_runs

    merged = merge_runs([clean_items([item("Board minutes", priority="low")], DOCS),
                         clean_items([item("Board minutes", priority="high")], DOCS)])
    assert merged[0]["priority"] == "high"


def test_single_sample_keeps_the_single_call_result_shape():
    out = run(determine_requirements(FakeGateway(content=reply([item("Odd one")])), DOCS, samples=1))
    assert [i["name"] for i in out["items"]] == ["Odd one"]
    assert "samples" not in out and "agreement" not in out["items"][0]


SEC_ORDER = {"name": "sec-order-latch.txt", "text": (
    "UNITED STATES OF AMERICA \nBefore the \nSECURITIES AND EXCHANGE COMMISSION \n \nADMINISTRATIVE PROCEEDING \n"
    "File No. 3-22767 \n \nIn the Matter of \n \nLATCH, INC., \n \nRespondent. \n \nORDER INSTITUTING CEASE-AND-\n"
    "DESIST PROCEEDINGS PURSUANT TO \nSECTION 8A OF THE SECURITIES ACT \nMAKING FINDINGS, AND IMPOSING A \n"
    "CEASE-AND-DESIST ORDER \n \nI. \nThe Securities and Exchange Commission deems it appropriate that proceedings "
    "be instituted against Latch, Inc. Respondent has submitted an Offer of Settlement which the Commission accepts.\n")}
RELEASE = {"name": "earnings-release.txt", "text": (
    "Exhibit 99.1\nPress Release\nFRANKLIN COVEY REPORTS FINANCIAL RESULTS FOR\nFOURTH QUARTER AND FULL FISCAL 2025\n"
    "CONDENSED CONSOLIDATED FINANCIAL STATEMENTS\nThe audited financial statements will be filed with the Form 10-K.\n")}
DISTRIBUTOR = {"name": "accuray-distributor-agreement.txt", "text": (
    "Exhibit 10.31    PURSUANT TO 17 C.F.R. 240.24B-2, CONFIDENTIAL INFORMATION HAS BEEN OMITTED FROM THIS DOCUMENT "
    "AND HAS BEEN FILED SEPARATELY WITH THE SECURITIES AND EXCHANGE COMMISSION PURSUANT TO A CONFIDENTIAL TREATMENT "
    "APPLICATION FILED WITH THE COMMISSION     ACCURAY INCORPORATED  MULTIPLE LINAC AND MULTI-MODALITY   DISTRIBUTOR "
    "AGREEMENT     This Distributor Agreement is entered into by Accuray and Siemens.")}
ROOM = [DISTRIBUTOR, SEC_ORDER, RELEASE]


def test_an_item_naming_a_supplied_document_is_marked_supplied():
    from packages.review.requirements import supplied_document

    assert supplied_document("SEC Cease-and-Desist Order (Latch, Inc.)", ROOM) == "sec-order-latch.txt"
    assert supplied_document("Distributor Agreement", ROOM) == "accuray-distributor-agreement.txt"
    assert supplied_document("Accuray distributor agreement", ROOM) == "accuray-distributor-agreement.txt"
    assert supplied_document("General ledger", DOCS) == "ledger.txt"
    items = clean_items([item("SEC Cease-and-Desist Order (Latch, Inc.)"),
                         item("Distributor Agreement", status="unclear")], ROOM)
    assert [i["status"] for i in items] == ["supplied", "supplied"]
    assert "sec-order-latch.txt" in items[0]["note"]


def test_supplied_document_check_does_not_fire_on_shared_generic_words():
    from packages.review.requirements import supplied_document

    for name in ["Audited financial statements", "Financial statements", "Form 10-K", "Offer of Settlement",
                 "Restated financial statements", "Unredacted Distributor Agreement", "Amendments to the Distributor Agreement",
                 "Confidential treatment application", "Board resolution approving the Distributor Agreement",
                 "Material customer contracts (Latch)", "SEC correspondence", "Earnings release for fiscal 2024"]:
        assert supplied_document(name, ROOM) == "", name
    assert clean_items([item("Audited financial statements")], ROOM)[0]["status"] == "missing"


def test_a_section_heading_or_a_sentence_near_the_top_is_not_a_supplied_document():
    from packages.review.requirements import supplied_document

    ten_k = [{"name": "annual.txt", "text": "FORM 10-K\nItem 8. Consolidated Financial Statements\n"
                                            "Our credit facility agreement with Big Bank was renewed.\n"}]
    assert supplied_document("Consolidated Financial Statements", ten_k) == ""
    assert supplied_document("Credit facility agreement", ten_k) == ""


def test_a_filing_form_in_an_exhibit_file_name_is_not_that_filing():
    from packages.review.requirements import supplied_document

    exhibit = [{"name": "Allied_20190815_8-K_EX-10.19_Content License Agreement.txt", "text": "JOINT CONTENT LICENSE AGREEMENT\n"}]
    assert supplied_document("Form 8-K", exhibit) == ""
    assert supplied_document("Content License Agreement", exhibit) != ""


def test_an_exhibit_or_schedule_of_a_supplied_document_is_not_that_document():
    """Seen live: "Exhibit A (Services Schedule)" was taken for the services agreement it belongs to."""
    from packages.review.requirements import supplied_document

    services = [{"name": "ABILITYINC_06_15_2020-EX-4.25-SERVICES AGREEMENT.txt", "text": "SERVICES AGREEMENT\nRECITALS\n"}]
    assert supplied_document("Services Agreement", services) != ""
    for name in ["Exhibit A (Services Schedule)", "Schedule 1 to the Services Agreement",
                 "Signature pages of the Services Agreement", "Side letters to the Services Agreement"]:
        assert supplied_document(name, services) == "", name


def test_long_structural_headings_and_contents_rows_are_found_and_matched():
    from packages.review.requirements import headings_of, section_match

    text = ("FORM 10-K\nItem 7. | Management's Discussion and Analysis of Financial Condition and Results of Operations\n"
            "Item 13. Certain Relationships and Related Transactions, and Director Independence\nOther text.\n")
    found = headings_of(text)
    assert "Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations" in found
    assert "Item 13. Certain Relationships and Related Transactions, and Director Independence" in found
    docs = [{"name": "10-K.txt", "text": text}]
    assert "Item 13." in section_match("Item 13 - Certain Relationships and Related Transactions", docs)
    assert section_match("Credit facility agreement", docs) == ""


def test_lease_expense_and_accounting_terms_get_sensible_workstreams():
    from packages.review.requirements import workstream_of

    assert workstream_of("Operating Expense Reconciliation Statements") == "real_estate"
    assert workstream_of("CAM reconciliation for 2024") == "real_estate"
    assert workstream_of("Rent Payment Ledger") == "real_estate"
    assert workstream_of("Bank reconciliation") == "financial"
    assert workstream_of("General ledger detail") == "financial"
    assert workstream_of("Monthly trial balance") == "financial"
    assert workstream_of("Book-to-tax reconciliation") == "tax"  # tax keeps priority
