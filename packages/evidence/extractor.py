import re
from decimal import Decimal
from typing import List, Dict, Any, Tuple
from packages.compute.lib import AddBackItem

_UNIT_MULTIPLIERS = {
    "thousand": Decimal(1_000), "k": Decimal(1_000),
    "million": Decimal(1_000_000), "m": Decimal(1_000_000), "mm": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000), "bn": Decimal(1_000_000_000),
}

class DocumentExtractionWorker:
    """
    Tier 2 Extraction Worker (Nemotron 3 Super capability).
    Parses unadjusted EBITDA, add-back claims, customer tables, and spans directly
    from raw document texts, validating exact verbatim spans in the source.
    """

    @staticmethod
    def extract_base_ebitda(cim_text: str) -> Tuple[Decimal, str]:
        """Extract reported unadjusted EBITDA and its verbatim span. A unit written after the figure ("$7.0 million",
        "$850k", "$1.2bn") is applied, so the value is always in units of currency."""
        amount = r"\$?([0-9,]+(?:\.[0-9]+)?)(?:[ \t]*(thousand|million|billion|mm|bn|k|m)\b)?"
        patterns = [
            r"Reported Unadjusted EBITDA:\s*" + amount,
            r"Base Operating EBITDA:\s*" + amount,
            r"Unadjusted EBITDA:\s*" + amount,
        ]
        for pat in patterns:
            match = re.search(pat, cim_text, re.IGNORECASE)
            if match:
                clean_num = match.group(1).replace(",", "")
                unit = (match.group(2) or "").lower()
                span = match.group(0)
                return Decimal(clean_num) * _UNIT_MULTIPLIERS.get(unit, Decimal(1)), span
        return Decimal("0.00"), ""

    @staticmethod
    def extract_addback_candidates(cim_text: str, accounts_text: str) -> List[AddBackItem]:
        """
        Extracts claimed add-backs from CIM and verifies them against the accounts ledger.
        Flags recurring patterns (e.g. costs appearing in multiple years) for Ultra adjudication.
        """
        candidates: List[AddBackItem] = []
        
        # Regex to locate bridge items:
        # Matches patterns like:
        # "1. Base Operating EBITDA: $7,000,000" (ignored, handled by base EBITDA)
        # "2. Management Restructuring & Severance: +$600,000 (One-time...)"
        # "- Add-back: Litigation Legal Fees: +$1,200,000 (Non-recurring...)"
        # An optional "Add-back:" lead-in before the title is not part of the title.
        pattern = (r"(?:(?:\d+\.|\-)\s*)(?:(?i:add[- ]?backs?)\s*:\s*)?([^\:\n]+):\s*\+\$?([0-9,]+(?:\.[0-9]+)?)"
                   r"\s*(?:\(([^)]+)\))?")
        matches = re.findall(pattern, cim_text)

        idx = 1
        for title, amt_str, rationale in matches:
            title_clean = title.strip()
            # Whole words: "Database Migration Costs" is an add-back, "Base Operating EBITDA" is not.
            if re.search(r"\b(?:base|ebitda)\b", title_clean, re.IGNORECASE):
                continue

            clean_amt = Decimal(amt_str.replace(",", ""))
            is_recurring = False
            evidence_span = ""
            source_doc = "Management Accounts"

            lowered = title_clean.lower()

            def has_word(words: List[str]) -> bool:
                # Whole words only: "it" must not match inside "Acquisition" or "Capital".
                return any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words)

            def ledger_line(text: str, pattern: str) -> str:
                # The first ledger line that shows the repeating cost: real text from the document, or "".
                m = re.search(rf"^.*(?:{pattern}).*$", text, re.IGNORECASE | re.MULTILINE)
                return m.group(0).strip() if m else ""

            # Check severance / restructuring / one-off separation
            if has_word(["severance", "restructuring", "separation", "realignment"]):
                note_match = re.search(r"(?:Note \d+|Executive|Workforce):[^\n]+", accounts_text)
                evidence_span = note_match.group(0) if note_match else ""
                is_recurring = False

            # Check legal / environmental / regulatory counsel
            elif has_word(["legal", "litigation", "compliance", "regulatory", "epa"]):
                # Check Table 1: Legal ledger
                table1_text = accounts_text[:accounts_text.find("Table 2")] if "Table 2" in accounts_text else accounts_text
                fy_matches = re.findall(r"(?:FY\d{2}|20\d{2})\s*(?:Legal|Retainer|Counsel)", table1_text, re.IGNORECASE)
                if len(fy_matches) >= 2 or "annual" in table1_text.lower():
                    is_recurring = True
                    note_match = re.search(r"(?:Audit Note \d+|Note \d+|Ledger):[^\n]+", table1_text)
                    evidence_span = note_match.group(0) if note_match else ledger_line(
                        table1_text, r"(?:FY\d{2}|20\d{2})\s*(?:Legal|Retainer|Counsel)")

            # Check software / ERP / modernization / sustaining tooling maintenance
            elif has_word(["erp", "it", "software", "modernization", "subscription", "maintenance", "tooling", "equipment"]):
                # Check Table 2: Maintenance ledger
                table2_text = accounts_text[accounts_text.find("Table 2"):] if "Table 2" in accounts_text else accounts_text
                fy_matches = re.findall(r"(?:FY\d{2}|20\d{2})\s*(?:SaaS|Cloud|ERP|Software|Tooling|Maintenance)", table2_text, re.IGNORECASE)
                if len(fy_matches) >= 2 or "annual" in table2_text.lower() or "recurring" in table2_text.lower():
                    is_recurring = True
                    note_match = re.search(r"(?:GL Account \d+|Account \d+|Ledger):[^\n]+", table2_text)
                    evidence_span = note_match.group(0) if note_match else ledger_line(
                        table2_text, r"(?:FY\d{2}|20\d{2})\s*(?:SaaS|Cloud|ERP|Software|Tooling|Maintenance)|recurring|annual")

            candidates.append(AddBackItem(
                id=f"AB-{idx}",
                description=title_clean,
                amount=clean_amt,
                claimed_rationale=rationale.strip() if rationale else "Seller claimed add-back",
                is_recurring=is_recurring,
                is_accepted=not is_recurring,
                evidence_span=evidence_span,
                source_document=source_doc
            ))
            idx += 1

        return candidates

    @staticmethod
    def extract_customer_concentration(waterfall_text: str) -> List[Dict[str, Any]]:
        """Parses customer table and calculates revenue distribution."""
        customers = []
        lines = waterfall_text.splitlines()
        for line in lines:
            # Anchored at the start of the line: a customer row begins with the customer's name. Searching
            # mid-line picked up fragments of ledger notes such as "GL Account 6420: Ongoing recurring: $300,000".
            match = re.match(
                r"^\s*(?:[-*]\s*)?(?:Client\s*\d+\s*\(([^)]+)\)|([A-Za-z0-9][A-Za-z0-9 &.\-]*?))\s*:\s*\$?([0-9,]+(?:\.[0-9]+)?)",
                line,
            )
            if match:
                name = (match.group(1) or match.group(2)).strip()
                amt_str = match.group(3).replace(",", "")
                rev = float(amt_str)
                # A "Total" line repeats the revenue and is not a customer at all. A bucket of many small accounts
                # ("Remaining SMB accounts") is real revenue but not one customer: it stays in the denominator
                # and is flagged so it is never ranked as a single customer.
                if re.search(r"\btotal\b", name, re.IGNORECASE):
                    continue
                # Ledger and note lines ("FY22 Legal Fees: $1,150,000", "Audit Note 12: ...") are not customers.
                if re.match(r"^\s*(?:FY\s?\d{2,4}|(?:19|20)\d{2})\b", name, re.IGNORECASE):
                    continue
                if re.search(r"\b(?:fees?|costs?|expenses?|ebitda|subscriptions?|maintenance|retainers?|"
                             r"audit note|gl account|table \d+|note \d+)\b", name, re.IGNORECASE):
                    continue
                is_bucket = bool(re.search(
                    r"\b(?:remaining|others?|smb|various|miscellaneous|sundry|all other|aggregate)\b",
                    name, re.IGNORECASE))
                if rev > 0:
                    customers.append({"name": name, "revenue": rev, "is_bucket": is_bucket})
        return customers
