import asyncio
import pytest
from arena.generator.adversarial import AdversarialSeller
from packages.workflow.pipeline import DiligencePipeline

@pytest.mark.asyncio
async def test_arena_adversarial_recall_saas_medium():
    """
    Evaluates PARSEC on the Medium Difficulty SaaS room (Titan Cloud).
    """
    room_data = AdversarialSeller.generate_adversarial_room(difficulty="medium")
    answer_key = room_data["answer_key"]
    
    pipeline = DiligencePipeline()
    result = await pipeline.run_pipeline({
        "company_name": "Titan Cloud Inc",
        "multiple": 11.0,
        "documents": room_data["documents"]
    })

    # 1. Verify Hero Moment Numbers
    hero = result["hero_metric"]
    assert hero["claimed_ebitda"] == answer_key["claimed_ebitda"]
    assert hero["parsec_ebitda"] == answer_key["real_ebitda"]
    assert hero["potential_overpayment_avoided"] == answer_key["total_overpayment_avoided"]

    # 2. Planted Issues Recall
    found_findings = result["findings"]
    legal_finding = next((f for f in found_findings if "Legal" in f["title"]), None)
    assert legal_finding is not None
    assert legal_finding["price_impact_usd"] == 13200000.00

    erp_finding = next((f for f in found_findings if "ERP" in f["title"] or "IT" in f["title"]), None)
    assert erp_finding is not None
    assert erp_finding["price_impact_usd"] == 13200000.00

    conc_finding = next((f for f in found_findings if "Concentration" in f["title"]), None)
    assert conc_finding is not None

    sec_finding = next((f for f in found_findings if "Prompt Injection" in f["title"]), None)
    assert sec_finding is not None

    # Cost / Token Budget Check
    meter = result["tokens_meter"]
    assert meter["total_cost_usd"] < 0.10

@pytest.mark.asyncio
async def test_arena_adversarial_recall_manufacturing_hard():
    """
    Evaluates PARSEC on the Hard Difficulty Manufacturing room (Vulcan Precision).
    """
    room_data = AdversarialSeller.generate_adversarial_room(difficulty="hard")
    answer_key = room_data["answer_key"]
    
    pipeline = DiligencePipeline()
    result = await pipeline.run_pipeline({
        "company_name": "Vulcan Precision LLC",
        "multiple": 8.5,
        "documents": room_data["documents"]
    })

    hero = result["hero_metric"]
    assert hero["claimed_ebitda"] == answer_key["claimed_ebitda"]
    assert hero["parsec_ebitda"] == answer_key["real_ebitda"]
    assert hero["potential_overpayment_avoided"] == answer_key["total_overpayment_avoided"]

    found_findings = result["findings"]
    assert len(found_findings) >= 3

    # Check that overpayment avoided was calculated exactly
    assert hero["potential_overpayment_avoided"] == 21250000.00
