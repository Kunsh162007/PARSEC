"""Reading an Adjusted EBITDA reconciliation: every line traced to the document, then checked by arithmetic.

The model is faked with canned replies. Accuracy on real earnings releases is measured by benchmarks/ebitda/run.py.
"""
import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

from packages.gateway.llm import GatewayUnavailable
from packages.tavily.client import RealityCheckResult
from packages.workflow.bridge import extract_bridge, row_values, verify_bridge
from packages.workflow.pipeline import DiligencePipeline

RELEASE = """Acme Corp reports third quarter results
Reconciliation of Net Income to Adjusted EBITDA (in thousands)
Three Months Ended | September 30, 2025 | September 30, 2024
Net income | $4,372 | $3,100
Interest expense, net | 1,210 | 1,180
Income tax expense | 950 | 700
Depreciation and amortization | 2,100 | 2,050
Stock-based compensation | 1,500 | 1,200
Restructuring costs | 1,585 | —
Acquisition-related costs | — | 400
Adjusted EBITDA | $11,717 | $8,630
"""


def line(label, value, row):
    return {"label": label, "value": value, "row": row}


def reply(total=11717, restructuring_row="Restructuring costs | 1,585 | —"):
    return json.dumps({
        "period": "Three months ended September 30, 2025", "units": "in thousands",
        "start": line("Net income", 4372, "Net income | $4,372 | $3,100"),
        "adjustments": [
            line("Interest expense, net", 1210, "Interest expense, net | $1,210 | $1,180"),  # "$" added by the model
            line("Income tax expense", 950, "Income tax expense | 950 | 700"),
            line("Depreciation and amortization", 2100, "Depreciation and amortization | 2,100 | 2,050"),
            line("Stock-based compensation", 1500, "Stock-based compensation | 1,500 | 1,200"),
            line("Restructuring costs", 1585, restructuring_row),
            line("Acquisition-related costs", 0, "Acquisition-related costs | — | 400"),
        ],
        "total": line("Adjusted EBITDA", total, f"Adjusted EBITDA | ${total:,} | $8,630"),
    })


class DownGateway:
    is_configured = False
    total_tokens_nano = total_tokens_super = total_tokens_ultra = 0
    total_cost_usd = 0.0

    async def complete(self, *a, **k):
        raise GatewayUnavailable("no key in test")


class BridgeGateway(DownGateway):
    is_configured = True

    async def complete(self, task, messages, **k):
        content = reply() if task == "ebitda_bridge_json" else '{"questions": []}'
        return SimpleNamespace(content=content, parsed_json=None, model_used="fake-super",
                               usage=SimpleNamespace(cost_usd=0.001))


class DownTavily:
    call_count = 0

    async def verify_external_claims(self, company, claims):
        return RealityCheckResult(query="q", available=False, reason="TAVILY_API_KEY is not set.")


def test_row_values_read_parentheses_dashes_and_skip_labels():
    assert row_values("Other income, net (1) | (5,695) | — | $(3,029)") == [Decimal("-5695"), Decimal("0"), Decimal("-3029")]


def test_a_reconciliation_whose_lines_are_all_in_the_document_and_add_up_foots():
    out = verify_bridge(reply(), RELEASE)
    assert out["status"] == "foots"
    assert out["computed_total"] == Decimal("11717") and out["difference"] == 0
    assert out["units"] == "in thousands" and len(out["adjustments"]) == 6


def test_every_checked_line_points_at_its_row_in_the_document():
    out = verify_bridge(reply(), RELEASE)
    s = out["adjustments"][0]
    assert RELEASE[s["start"]:s["end"]].startswith("Interest expense, net | 1,210")


def test_a_total_that_the_lines_do_not_reach_is_reported_not_hidden():
    text = RELEASE.replace("$11,717", "$12,717")
    out = verify_bridge(reply(total=12717), text)
    assert out["status"] == "does_not_foot" and out["difference"] == Decimal("1000")


def test_a_value_that_is_not_in_the_document_makes_the_reading_incomplete():
    out = verify_bridge(reply(restructuring_row="Restructuring costs | 9,999 | —").replace("1585", "9999"), RELEASE)
    assert out["status"] == "incomplete"
    assert out["unverified"][0]["label"] == "Restructuring costs"


def test_no_reconciliation_and_an_unreadable_reply_are_stated():
    assert verify_bridge('{"none": true}', RELEASE)["status"] == "incomplete"
    assert verify_bridge("I could not find it.", RELEASE)["status"] == "unavailable"


def test_extract_bridge_reports_a_missing_model_instead_of_guessing():
    assert asyncio.run(extract_bridge(DownGateway(), RELEASE))["status"] == "unavailable"


def run_pipeline(gateway):
    pipeline = DiligencePipeline(gateway=gateway, tavily=DownTavily())
    return asyncio.run(pipeline.run_pipeline({"company_name": "Acme Corp", "multiple": 10, "documents": {"release.txt": RELEASE}}))


def test_pipeline_reads_a_real_layout_reconciliation_and_lists_the_add_backs_to_test():
    out = run_pipeline(BridgeGateway())
    ids = {f["id"]: f for f in out["findings"]}
    assert "FINDING-BASE-EBITDA-MISSING" not in ids
    assert ids["FINDING-EBITDA-RECON"]["evidence"][0]["exact_text_quote"].startswith("Net income | $4,372")
    addbacks = ids["FINDING-ADDBACKS-TO-TEST"]
    assert "Stock-based compensation 1500" in addbacks["description"] and "Restructuring costs 1585" in addbacks["description"]
    assert "Interest" not in addbacks["description"].split("adds back:")[1]
    assert addbacks["price_impact_usd"] == 3085 * 1000 * 10
    assert out["ebitda_reconciliation"]["status"] == "foots"


def test_pipeline_without_a_model_says_what_it_could_not_do():
    out = run_pipeline(DownGateway())
    ids = [f["id"] for f in out["findings"]]
    assert "FINDING-BASE-EBITDA-MISSING" in ids
    assert any("reconciliation" in u for u in out["unavailable"])


def test_a_reconciliation_set_as_running_text_is_traced_by_its_first_figure_only():
    from packages.workflow.bridge import _find_in_running_text

    text = "Adjusted EBITDA reconciliation Debt amendment costs - 37 - 781 ERP Implementation costs 178 185 882 241"
    assert _find_in_running_text(text, "Debt amendment costs", Decimal("0")) is not None
    assert _find_in_running_text(text, "ERP Implementation costs", Decimal("178")) is not None
    assert _find_in_running_text(text, "ERP Implementation costs", Decimal("185")) is None  # a prior-period column


def test_an_add_back_present_in_every_period_column_is_flagged_and_a_one_off_is_not():
    from packages.workflow.bridge import recurs_in_every_period

    out = verify_bridge(reply(), RELEASE)
    by_label = {a["label"]: a for a in out["adjustments"]}
    assert by_label["Stock-based compensation"]["periods"] == [Decimal("1500"), Decimal("1200")]
    assert recurs_in_every_period(by_label["Stock-based compensation"]) is True
    assert recurs_in_every_period(by_label["Restructuring costs"]) is False  # a dash in the prior period


def test_ledger_years_finds_a_cost_booked_in_several_years():
    from packages.workflow.bridge import ledger_years

    ledger = "Management accounts\nFY22 Stock-based compensation expense 1,100,000\nFY23 Stock-based compensation expense 1,300,000\nRent 2023 400,000"
    found = ledger_years("Stock-based compensation", {"release.txt": RELEASE, "ledger.txt": ledger}, skip="release.txt")
    assert found["years"] == ["2022", "2023"]
    assert ledger_years("Building exit costs", {"ledger.txt": ledger})["years"] == []


def test_pipeline_flags_recurring_add_backs_and_ledger_matches_with_evidence():
    ledger = "Management accounts\nFY22 Stock-based compensation expense 1,100,000\nFY23 Stock-based compensation expense 1,300,000"
    pipeline = DiligencePipeline(gateway=BridgeGateway(), tavily=DownTavily())
    out = asyncio.run(pipeline.run_pipeline({"company_name": "Acme Corp", "multiple": 10,
                                             "documents": {"release.txt": RELEASE, "ledger.txt": ledger}}))
    ids = {f["id"]: f for f in out["findings"]}
    recurs = ids["FINDING-ADDBACK-RECURS"]
    assert "Stock-based compensation (1500, 1200)" in recurs["description"] and "Restructuring" not in recurs["description"]
    assert recurs["price_impact_usd"] == 1500 * 1000 * 10
    in_ledger = ids["FINDING-ADDBACK-IN-LEDGER"]
    assert "Stock-based compensation in 2022, 2023" in in_ledger["description"]
    assert in_ledger["evidence"][0]["document_id"] == "ledger.txt"
