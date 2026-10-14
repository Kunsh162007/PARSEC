from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

def to_decimal(val: Any) -> Decimal:
    """Helper to safely convert float/int/str to Decimal with 2 decimal places."""
    if isinstance(val, Decimal):
        return val
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class AddBackItem(BaseModel):
    id: str
    description: str
    amount: Decimal
    claimed_rationale: str
    is_recurring: bool = False
    is_accepted: bool = False
    evidence_span: Optional[str] = None
    source_document: Optional[str] = None

class EBITDABridgeResult(BaseModel):
    unadjusted_ebitda: Decimal
    seller_claimed_ebitda: Decimal
    parsec_adjudicated_ebitda: Decimal
    total_claimed_addbacks: Decimal
    total_accepted_addbacks: Decimal
    total_rejected_addbacks: Decimal
    delta_ebitda: Decimal  # seller_claimed - parsec_adjudicated
    multiple: Decimal
    price_impact_overpayment: Decimal  # delta_ebitda * multiple
    waterfall_steps: List[Dict[str, Any]]

class ConcentrationResult(BaseModel):
    top_customer_pct: Decimal
    top_3_pct: Decimal
    top_5_pct: Decimal
    herfindahl_index: Decimal
    is_concentration_critical: bool
    is_concentration_warning: bool
    customer_breakdown: List[Dict[str, Any]]

class FootingCheckResult(BaseModel):
    passed: bool
    discrepancies: List[Dict[str, Any]]

class ComputeLib:
    """
    Pure deterministic financial computation engine.
    ZERO LLM Hallucination. Every operation executed via Decimal.
    """

    @staticmethod
    def calculate_ebitda_bridge(
        unadjusted_ebitda: Decimal | float,
        add_backs: List[AddBackItem],
        multiple: Decimal | float = 11.0
    ) -> EBITDABridgeResult:
        unadj = to_decimal(unadjusted_ebitda)
        mult = to_decimal(multiple)

        claimed_total = Decimal("0.00")
        accepted_total = Decimal("0.00")
        rejected_total = Decimal("0.00")

        waterfall_steps = [
            {"step": "Reported Unadjusted EBITDA", "amount": float(unadj), "cumulative": float(unadj)}
        ]

        running_adjudicated = unadj
        running_claimed = unadj

        for item in add_backs:
            amt = to_decimal(item.amount)
            claimed_total += amt
            running_claimed += amt
            
            if item.is_accepted:
                accepted_total += amt
                running_adjudicated += amt
                waterfall_steps.append({
                    "step": f"Accepted: {item.description}",
                    "amount": float(amt),
                    "accepted": True,
                    "cumulative": float(running_adjudicated)
                })
            else:
                rejected_total += amt
                waterfall_steps.append({
                    "step": f"Disallowed (Recurring/Unsubstantiated): {item.description}",
                    "amount": float(-amt),
                    "accepted": False,
                    "cumulative": float(running_adjudicated)
                })

        seller_claimed = unadj + claimed_total
        delta_ebitda = seller_claimed - running_adjudicated
        price_impact = (delta_ebitda * mult).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return EBITDABridgeResult(
            unadjusted_ebitda=unadj,
            seller_claimed_ebitda=seller_claimed,
            parsec_adjudicated_ebitda=running_adjudicated,
            total_claimed_addbacks=claimed_total,
            total_accepted_addbacks=accepted_total,
            total_rejected_addbacks=rejected_total,
            delta_ebitda=delta_ebitda,
            multiple=mult,
            price_impact_overpayment=price_impact,
            waterfall_steps=waterfall_steps
        )

    @staticmethod
    def calculate_customer_concentration(
        customers: List[Dict[str, Any]]  # [{"name": "Acme", "revenue": 1000000}]
    ) -> ConcentrationResult:
        if not customers:
            return ConcentrationResult(
                top_customer_pct=Decimal("0.00"),
                top_3_pct=Decimal("0.00"),
                top_5_pct=Decimal("0.00"),
                herfindahl_index=Decimal("0.00"),
                is_concentration_critical=False,
                is_concentration_warning=False,
                customer_breakdown=[]
            )

        # Buckets ("Remaining SMB accounts") are real revenue, so they stay in the denominator, but they are
        # many customers, never one, so they are not ranked.
        total_rev = sum(to_decimal(c["revenue"]) for c in customers)
        if total_rev == Decimal("0.00"):
            total_rev = Decimal("1.00")

        named = [c for c in customers if not c.get("is_bucket")]
        if not named:
            return ConcentrationResult(
                top_customer_pct=Decimal("0.00"), top_3_pct=Decimal("0.00"), top_5_pct=Decimal("0.00"),
                herfindahl_index=Decimal("0.00"), is_concentration_critical=False,
                is_concentration_warning=False, customer_breakdown=[],
            )

        # Sort descending by revenue
        sorted_custs = sorted(named, key=lambda x: to_decimal(x["revenue"]), reverse=True)
        breakdown = []
        hhi = Decimal("0.0000")

        for c in sorted_custs:
            rev = to_decimal(c["revenue"])
            share = (rev / total_rev).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            hhi += (share * 100) ** 2
            breakdown.append({
                "name": c["name"],
                "revenue": float(rev),
                "share_pct": float(share * 100)
            })

        top_1 = to_decimal(breakdown[0]["share_pct"]) if breakdown else Decimal("0.00")
        top_3 = to_decimal(sum(b["share_pct"] for b in breakdown[:3]))
        top_5 = to_decimal(sum(b["share_pct"] for b in breakdown[:5]))

        return ConcentrationResult(
            top_customer_pct=top_1,
            top_3_pct=top_3,
            top_5_pct=top_5,
            herfindahl_index=hhi.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            is_concentration_critical=top_1 >= Decimal("35.0"),
            is_concentration_warning=top_1 >= Decimal("20.0"),
            customer_breakdown=breakdown
        )

    @staticmethod
    def verify_footing(
        line_items: List[Decimal | float],
        reported_total: Decimal | float,
        tolerance: Decimal = Decimal("0.02")
    ) -> FootingCheckResult:
        computed_total = sum(to_decimal(item) for item in line_items)
        rep = to_decimal(reported_total)
        diff = abs(computed_total - rep)
        passed = diff <= tolerance

        discrepancies = []
        if not passed:
            discrepancies.append({
                "reported_total": float(rep),
                "computed_sum": float(computed_total),
                "discrepancy": float(diff),
                "error": f"Sum of line items ({computed_total}) does not equal reported total ({rep})"
            })

        return FootingCheckResult(passed=passed, discrepancies=discrepancies)

    @staticmethod
    def calculate_nwc(
        current_assets_ex_cash: Decimal | float,
        current_liabilities_ex_debt: Decimal | float,
        target_peg: Decimal | float
    ) -> Dict[str, Any]:
        ca = to_decimal(current_assets_ex_cash)
        cl = to_decimal(current_liabilities_ex_debt)
        nwc = ca - cl
        peg = to_decimal(target_peg)
        deficit_surplus = nwc - peg

        return {
            "current_nwc": float(nwc),
            "target_peg": float(peg),
            "deficit_or_surplus": float(deficit_surplus),
            "purchase_price_adjustment": float(deficit_surplus)  # Deficit reduces cash paid by buyer
        }
