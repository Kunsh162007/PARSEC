from decimal import Decimal

from packages.evidence.extractor import DocumentExtractionWorker as Extractor

CIM = """Reported Unadjusted EBITDA: $7,000,000
MANAGEMENT ADJUSTED EBITDA BRIDGE:
1. Base Operating EBITDA: $7,000,000
2. Specialized Legal Defense Costs: +$1,200,000 (Non-recurring litigation legal representation)
3. Aborted Acquisition Advisory Fees: +$400,000 (One-time costs of an aborted transaction)
"""

ACCOUNTS = """Table 1: Operating Expense Detailed Ledger
FY22 Legal Fees (General Retainers): $1,150,000
FY23 Legal Fees (General Retainers): $1,180,000
FY24 Legal Fees (General Retainers): $1,200,000
Audit Note 12: Advisory fees are ongoing annual retainers with external counsel.

Table 2: Software & IT Systems Maintenance
FY24 Note 4: nothing recurring here
"""


def test_base_ebitda_is_found_with_its_verbatim_span():
    value, span = Extractor.extract_base_ebitda(CIM)
    assert value == Decimal("7000000")
    assert span in CIM


def test_base_ebitda_not_found_is_zero_with_no_span_and_no_invented_default():
    value, span = Extractor.extract_base_ebitda("Revenue was $50,000,000 and the company is well run.")
    assert value == Decimal("0.00")
    assert span == ""


def test_recurring_legal_addback_is_flagged_with_text_taken_from_the_ledger():
    items = Extractor.extract_addback_candidates(CIM, ACCOUNTS)
    legal = next(i for i in items if "Legal" in i.description)
    assert legal.is_recurring is True
    assert legal.evidence_span in ACCOUNTS
    assert legal.evidence_span  # something real, never a made-up phrase


def test_the_word_it_inside_other_words_does_not_make_a_cost_an_it_cost():
    items = Extractor.extract_addback_candidates(CIM, ACCOUNTS)
    deal = next(i for i in items if "Acquisition" in i.description)
    assert deal.is_recurring is False
    assert deal.is_accepted is True


def test_evidence_is_empty_rather_than_invented_when_the_ledger_has_none():
    cim = "1. Management Restructuring & Severance: +$600,000 (One-time separation)\n"
    items = Extractor.extract_addback_candidates(cim, "No relevant notes here.")
    assert items[0].evidence_span == ""


def test_every_evidence_span_the_extractor_returns_exists_in_the_source():
    items = Extractor.extract_addback_candidates(CIM, ACCOUNTS)
    for item in items:
        assert item.evidence_span == "" or item.evidence_span in ACCOUNTS


def test_a_bucket_of_many_small_accounts_is_not_a_single_customer():
    text = ("Client 101 (Atlas Retail): $1,000,000 (10.0%)\n"
            "Remaining SMB accounts: $9,000,000 (90.0%)\n"
            "Other customers: $500,000\n")
    rows = Extractor.extract_customer_concentration(text)
    assert [c["name"] for c in rows if not c["is_bucket"]] == ["Atlas Retail"]
    assert {c["name"] for c in rows if c["is_bucket"]} == {"Remaining SMB accounts", "Other customers"}


def test_customer_table_is_parsed_and_totals_are_not_customers():
    text = "Total Enterprise Revenue: $10,000,000\nClient 101 (Atlas Retail): $4,100,000 (41.0%)\nClient 102 (Nordic): $1,900,000 (19.0%)\n"
    customers = Extractor.extract_customer_concentration(text)
    assert [c["name"] for c in customers] == ["Atlas Retail", "Nordic"]
    assert not any(c["is_bucket"] for c in customers)


def test_ledger_and_note_lines_are_never_customers():
    text = ("FY22 Legal Fees (General Retainers): $1,150,000\n"
            "FY24 SaaS Cloud & ERP Subscriptions: $1,200,000\n"
            "Audit Note 12: Advisory fees: $50,000\n"
            "GL Account 6420: Ongoing recurring: $300,000\n"
            "Total operating costs: $9,000,000\n")
    assert Extractor.extract_customer_concentration(text) == []
