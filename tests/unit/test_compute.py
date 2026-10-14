from packages.compute.lib import ComputeLib, AddBackItem
from decimal import Decimal

def test_ebitda_bridge_hero_moment():
    # Hero moment verification:
    # Claimed: $10.0M EBITDA
    # Unadjusted reported: $7.0M
    # Add-backs:
    # 1. Severance / Restructuring: $600k (one-off, legitimate -> accepted)
    # 2. "One-off" legal fees: $1.2M (found to recur across FY22, FY23, FY24 -> disallowed)
    # 3. "Software transition cost": $1.2M (recurring operational IT spend -> disallowed)
    # Real EBITDA: $7.0M + $0.6M = $7.6M
    # Claimed EBITDA: $7.0M + $0.6M + $1.2M + $1.2M = $10.0M
    # Delta: $2.4M
    # At 11.0x multiple: 2.4M * 11 = $26.4M overpayment avoided!

    addbacks = [
        AddBackItem(
            id="AB-1",
            description="Executive severance & restructuring",
            amount=Decimal("600000.00"),
            claimed_rationale="One-time separation agreement",
            is_recurring=False,
            is_accepted=True,
            evidence_span="Management Accounts Note 4: One-time payment to departing COO"
        ),
        AddBackItem(
            id="AB-2",
            description="IP Litigation legal fees",
            amount=Decimal("1200000.00"),
            claimed_rationale="Non-recurring legal defense",
            is_recurring=True,
            is_accepted=False,
            evidence_span="Audit Report Note 12: Recurring annual IP retainer"
        ),
        AddBackItem(
            id="AB-3",
            description="ERP software transition costs",
            amount=Decimal("1200000.00"),
            claimed_rationale="One-time digital transformation expense",
            is_recurring=True,
            is_accepted=False,
            evidence_span="GL Account 6420: Ongoing SaaS subscription and maintenance"
        )
    ]

    result = ComputeLib.calculate_ebitda_bridge(
        unadjusted_ebitda=Decimal("7000000.00"),
        add_backs=addbacks,
        multiple=Decimal("11.0")
    )

    assert result.seller_claimed_ebitda == Decimal("10000000.00")
    assert result.parsec_adjudicated_ebitda == Decimal("7600000.00")
    assert result.delta_ebitda == Decimal("2400000.00")
    assert result.price_impact_overpayment == Decimal("26400000.00")
    assert len(result.waterfall_steps) == 4

def test_customer_concentration_critical_flag():
    customers = [
        {"name": "Alpha Corp", "revenue": 4100000.00},  # 41%
        {"name": "Beta Inc", "revenue": 1900000.00},
        {"name": "Gamma LLC", "revenue": 1500000.00},
        {"name": "Delta Co", "revenue": 1500000.00},
        {"name": "Others", "revenue": 1000000.00},
    ]

    result = ComputeLib.calculate_customer_concentration(customers)
    assert result.top_customer_pct == Decimal("41.00")
    assert result.is_concentration_critical is True
    assert result.is_concentration_warning is True

def test_footing_reconciliation():
    line_items = [100.50, 200.25, 300.25]
    # Correct sum: 601.00
    res_pass = ComputeLib.verify_footing(line_items, 601.00)
    assert res_pass.passed is True

    # Failed sum (e.g. seller hides $50 mismatch)
    res_fail = ComputeLib.verify_footing(line_items, 651.00)
    assert res_fail.passed is False
    assert len(res_fail.discrepancies) == 1


def test_concentration_denominator_includes_buckets_but_they_are_never_ranked():
    from packages.compute.lib import ComputeLib

    customers = [
        {"name": "Atlas", "revenue": 4_100_000},
        {"name": "Nordic", "revenue": 1_900_000},
        {"name": "Remaining SMB accounts", "revenue": 4_000_000, "is_bucket": True},
    ]
    result = ComputeLib.calculate_customer_concentration(customers)
    assert float(result.top_customer_pct) == 41.0           # 4.1M of 10.0M total, not of 6.0M
    assert result.is_concentration_critical is True
    assert all(b["name"] != "Remaining SMB accounts" for b in result.customer_breakdown)


def test_only_buckets_means_no_named_customer_to_rank():
    from packages.compute.lib import ComputeLib

    result = ComputeLib.calculate_customer_concentration(
        [{"name": "All other accounts", "revenue": 1_000_000, "is_bucket": True}])
    assert float(result.top_customer_pct) == 0.0
    assert result.is_concentration_critical is False
