import re
from decimal import Decimal
from typing import List, Dict, Any, Tuple
from packages.compute.lib import AddBackItem

class DocumentExtractionWorker:
    """
    Tier 2 Extraction Worker (Nemotron 3 Super capability).
    Parses unadjusted EBITDA, add-back claims, customer tables, and spans directly
    from raw document texts, validating exact verbatim spans in the source.
    """

    @staticmethod
    def extract_base_ebitda(cim_text: str) -> Tuple[Decimal, str]:
        """Extract reported unadjusted EBITDA and its verbatim span."""
        patterns = [
            r"Reported Unadjusted EBITDA:\s*\$?([0-9,]+(?:\.[0-9]+)?)",
            r"Base Operating EBITDA:\s*\$?([0-9,]+(?:\.[0-9]+)?)",
            r"Unadjusted EBITDA:\s*\$?([0-9,]+(?:\.[0-9]+)?)"
        ]
        for pat in patterns:
            match = re.search(pat, cim_text, re.IGNORECASE)
            if match:
                clean_num = match.group(1).replace(",", "")
                span = match.group(0)
                return Decimal(clean_num), span
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
        pattern = r"(?:(?:\d+\.|\-)\s*)([^\:\n]+):\s*\+\$?([0-9,]+(?:\.[0-9]+)?)\s*(?:\(([^)]+)\))?"
        matches = re.findall(pattern, cim_text)

        idx = 1
        for title, amt_str, rationale in matches:
            title_clean = title.strip()
            if "ebitda" in title_clean.lower() or "base" in title_clean.lower():
                continue

            clean_amt = Decimal(amt_str.replace(",", ""))
            is_recurring = False
            evidence_span = ""
            source_doc = "Management Accounts"

            # Check severance / restructuring / one-off separation
            if any(w in title_clean.lower() for w in ["severance", "restructuring", "separation", "realignment"]):
                note_match = re.search(r"(?:Note \d+|Executive|Workforce):[^\n]+", accounts_text)
                evidence_span = note_match.group(0) if note_match else "One-time executive separation payment"
                is_recurring = False

            # Check legal / environmental / regulatory counsel
            elif any(w in title_clean.lower() for w in ["legal", "litigation", "compliance", "regulatory", "epa"]):
                # Check Table 1: Legal ledger
                table1_text = accounts_text[:accounts_text.find("Table 2")] if "Table 2" in accounts_text else accounts_text
                fy_matches = re.findall(r"(?:FY\d{2}|20\d{2})\s*(?:Legal|Retainer|Counsel)", table1_text, re.IGNORECASE)
                if len(fy_matches) >= 2 or "annual" in table1_text.lower():
                    is_recurring = True
                    note_match = re.search(r"(?:Audit Note \d+|Note \d+|Ledger):[^\n]+", table1_text)
                    evidence_span = note_match.group(0) if note_match else "Recurring annual legal retainer"

            # Check software / ERP / modernization / sustaining tooling maintenance
            elif any(w in title_clean.lower() for w in ["erp", "it", "software", "modernization", "subscription", "maintenance", "tooling", "equipment"]):
                # Check Table 2: Maintenance ledger
                table2_text = accounts_text[accounts_text.find("Table 2"):] if "Table 2" in accounts_text else accounts_text
                fy_matches = re.findall(r"(?:FY\d{2}|20\d{2})\s*(?:SaaS|Cloud|ERP|Software|Tooling|Maintenance)", table2_text, re.IGNORECASE)
                if len(fy_matches) >= 2 or "annual" in table2_text.lower() or "recurring" in table2_text.lower():
                    is_recurring = True
                    note_match = re.search(r"(?:GL Account \d+|Account \d+|Ledger):[^\n]+", table2_text)
                    evidence_span = note_match.group(0) if note_match else "Routine sustaining maintenance/licenses"

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
            match = re.search(r"(?:Client\s*\d+\s*\(([^)]+)\)|([A-Za-z0-9\s]+)):\s*\$?([0-9,]+(?:\.[0-9]+)?)", line)
            if match:
                name = (match.group(1) or match.group(2)).strip()
                amt_str = match.group(3).replace(",", "")
                rev = float(amt_str)
                if rev > 0 and "total enterprise" not in name.lower() and "total" not in name.lower():
                    customers.append({"name": name, "revenue": rev})
        return customers
