from packages.evidence.forensics import ForensicAccountingEngine

def test_benfords_law_detects_fabricated_financial_numbers():
    # Fabricated numbers where a human seller systematically typed figures starting with 7, 8, 9
    fabricated_ledger = [
        78400.0, 79200.0, 81000.0, 82500.0, 73000.0, 88000.0, 91000.0, 94000.0,
        76000.0, 75000.0, 89000.0, 92000.0, 83000.0, 87000.0, 96000.0, 79000.0,
        84000.0, 91500.0, 83500.0, 78500.0
    ]
    
    result = ForensicAccountingEngine.test_benfords_law(fabricated_ledger)
    # Chi-square stat will heavily exceed the 15.51 critical threshold
    assert result.is_anomaly_detected is True
    assert result.chi_square_stat > 15.51

def test_benfords_law_passes_organic_financial_distribution():
    # Natural scale-invariant financial distribution (following 1/x Benford distribution)
    organic_ledger = [
        1100.0, 1250.0, 1400.0, 1800.0, 1950.0, 1050.0, 1300.0, 1700.0, 1600.0,
        2100.0, 2400.0, 2800.0, 2900.0, 2200.0,
        3100.0, 3500.0, 3800.0,
        4200.0, 4800.0,
        5100.0, 5600.0,
        6200.0, 7100.0, 8300.0, 9200.0
    ]

    result = ForensicAccountingEngine.test_benfords_law(organic_ledger)
    assert result.is_anomaly_detected is False

def test_proof_of_cash_flags_phantom_earnings():
    # Seller reports $10M EBITDA, but Cash Flow from Ops is only $1.8M and bank balance fell by $500k
    result = ForensicAccountingEngine.verify_proof_of_cash(
        reported_ebitda=10000000.00,
        cash_flow_from_operations=1800000.00,
        working_capital_change=-200000.00,
        cash_taxes_paid=400000.00,
        actual_ending_bank_cash=1500000.00,
        actual_beginning_bank_cash=2000000.00
    )

    assert result.reconciled is False
    assert len(result.red_flags) >= 2
    assert any("Severe cash conversion disconnect" in rf for rf in result.red_flags)
    assert any("Phantom Cash Divergence" in rf for rf in result.red_flags)

def test_shell_company_detection():
    # Customer represents 41% of revenue, zero web presence, shared address
    res = ForensicAccountingEngine.detect_shell_company_indicators(
        customer_name="Apex Global Corp (Disguised Affiliate)",
        tavily_web_signals={"results": []}, # zero web presence
        incorporation_days_before_deal=45,
        shared_address_with_target=True,
        revenue_share_pct=41.0
    )

    assert res["is_suspected_shell"] is True
    assert res["risk_level"] == "CRITICAL"
    assert len(res["indicators"]) == 3
