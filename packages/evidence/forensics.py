import math
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any, Tuple
from pydantic import BaseModel
from packages.compute.lib import to_decimal

class BenfordAnalysisResult(BaseModel):
    is_anomaly_detected: bool
    chi_square_stat: float
    critical_value_95: float = 15.51 # 8 degrees of freedom at p=0.05
    leading_digit_distribution: Dict[int, float]
    benford_expected_distribution: Dict[int, float]
    divergent_digits: List[int]

class ProofOfCashResult(BaseModel):
    reconciled: bool
    reported_ebitda: float
    reported_cfo: float  # Cash Flow from Operations
    actual_bank_cash_increase: float
    unexplained_variance: float
    red_flags: List[str]

class ForensicAccountingEngine:
    """
    Forensic Auditing Module for detecting:
    1. Fabricated Numbers & Fraud via Benford's Law (First-Digit Analysis)
    2. Proof-of-Cash Disconnect (P&L EBITDA vs Cash Flow from Ops vs Actual Bank Deposits)
    3. Shell Company / Undisclosed Related-Party Detection
    4. Channel-Stuffing / Period-End Revenue Spikes
    """

    BENFORD_PROBABILITIES = {
        1: 0.301, 2: 0.176, 3: 0.125, 4: 0.097,
        5: 0.079, 6: 0.067, 7: 0.058, 8: 0.051, 9: 0.046
    }

    @classmethod
    def test_benfords_law(cls, numbers: List[float | Decimal]) -> BenfordAnalysisResult:
        """
        Tests financial line items against Benford's Law distribution.
        Human-fabricated or artificially padded accounting ledgers statistically
        over-index on specific digits (e.g. 7, 8, 9) and fail Chi-Square goodness-of-fit.
        """
        valid_digits = []
        for n in numbers:
            val = abs(float(n))
            if val >= 1.0:
                s = f"{val:.4f}".replace(".", "").lstrip("0")
                if s:
                    first_digit = int(s[0])
                    if 1 <= first_digit <= 9:
                        valid_digits.append(first_digit)

        total_obs = len(valid_digits)
        if total_obs < 15:
            # Sample too small for statistical rejection
            return BenfordAnalysisResult(
                is_anomaly_detected=False,
                chi_square_stat=0.0,
                leading_digit_distribution={},
                benford_expected_distribution=cls.BENFORD_PROBABILITIES,
                divergent_digits=[]
            )

        counts = {d: 0 for d in range(1, 10)}
        for d in valid_digits:
            counts[d] += 1

        observed_dist = {d: counts[d] / total_obs for d in range(1, 10)}
        chi_square = 0.0
        divergent = []

        for d in range(1, 10):
            observed = counts[d]
            expected = total_obs * cls.BENFORD_PROBABILITIES[d]
            chi_square += ((observed - expected) ** 2) / expected
            
            # Check individual digit relative variance (> 80% deviation)
            if abs(observed_dist[d] - cls.BENFORD_PROBABILITIES[d]) / cls.BENFORD_PROBABILITIES[d] > 0.8:
                divergent.append(d)

        # 8 degrees of freedom at 95% confidence level critical value is 15.51
        is_anomaly = chi_square > 15.51

        return BenfordAnalysisResult(
            is_anomaly_detected=is_anomaly,
            chi_square_stat=round(chi_square, 2),
            leading_digit_distribution={k: round(v, 3) for k, v in observed_dist.items()},
            benford_expected_distribution=cls.BENFORD_PROBABILITIES,
            divergent_digits=divergent
        )

    @classmethod
    def verify_proof_of_cash(
        cls,
        reported_ebitda: float | Decimal,
        cash_flow_from_operations: float | Decimal,
        working_capital_change: float | Decimal,
        cash_taxes_paid: float | Decimal,
        actual_ending_bank_cash: float | Decimal,
        actual_beginning_bank_cash: float | Decimal,
        tolerance_pct: float = 0.10
    ) -> ProofOfCashResult:
        """
        Proof of Cash reconciles Paper Profits (EBITDA) with Reality (Cash in Bank).
        Junior analysts look at reported EBITDA. Senior forensic partners trace to bank statements.
        Fraudulent companies frequently show high EBITDA but persistent zero/negative cash conversion.
        """
        ebitda = float(reported_ebitda)
        cfo = float(cash_flow_from_operations)
        net_bank_change = float(actual_ending_bank_cash) - float(actual_beginning_bank_cash)

        red_flags = []
        
        # 1. Cash Conversion Ratio: CFO / EBITDA
        if ebitda > 0:
            conversion_ratio = cfo / ebitda
            if conversion_ratio < 0.40:
                red_flags.append(
                    f"Severe cash conversion disconnect: CFO (${cfo:,.0f}) is only {conversion_ratio*100:.1f}% of reported EBITDA (${ebitda:,.0f}). Earnings quality suspect."
                )

        # 2. Operating Cash vs Actual Bank Account Movement
        expected_operating_cash = cfo
        variance = abs(cfo - net_bank_change)
        
        if cfo > 0 and net_bank_change < 0:
            red_flags.append(
                f"Phantom Cash Divergence: Company claims ${cfo:,.0f} in operating cash, but bank balances decreased by ${abs(net_bank_change):,.0f}."
            )

        reconciled = len(red_flags) == 0

        return ProofOfCashResult(
            reconciled=reconciled,
            reported_ebitda=ebitda,
            reported_cfo=cfo,
            actual_bank_cash_increase=net_bank_change,
            unexplained_variance=variance,
            red_flags=red_flags
        )

    @classmethod
    def detect_shell_company_indicators(
        cls,
        customer_name: str,
        tavily_web_signals: Dict[str, Any],
        incorporation_days_before_deal: int = 0,
        shared_address_with_target: bool = False,
        revenue_share_pct: float = 0.0
    ) -> Dict[str, Any]:
        """
        Detects fictitious or shell entities masking circular revenue / related party transactions.
        Red flags include:
        - No active web presence / corporate footprint on Tavily
        - Formed shortly (< 180 days) prior to the transaction
        - Shared registered agent / physical address with target company or executives
        """
        indicators = []
        is_suspicious = False

        if shared_address_with_target:
            indicators.append("Entity shares identical registered corporate address with target company or executive")
            is_suspicious = True

        if 0 < incorporation_days_before_deal < 180:
            indicators.append(f"Entity incorporated only {incorporation_days_before_deal} days prior to deal launch")
            is_suspicious = True

        # Check Tavily external signals for web presence
        results = tavily_web_signals.get("results", [])
        if len(results) == 0:
            indicators.append(f"Zero verified digital footprint or commercial activity found on public web for '{customer_name}'")
            if revenue_share_pct > 5.0:
                is_suspicious = True
        
        return {
            "entity_name": customer_name,
            "is_suspected_shell": is_suspicious,
            "risk_level": "CRITICAL" if is_suspicious and revenue_share_pct > 10.0 else ("WARNING" if is_suspicious else "LOW"),
            "indicators": indicators
        }
