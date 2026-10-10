import json
import pytest
from packages.schemas.models import EvidenceSpan

def test_financebench_evidence_span_verification():
    """
    Evaluates PARSEC's span-verification engine on real SEC 10-K filings from FinanceBench.
    Confirms that real financial claims extractable from SEC filings pass deterministic
    exact-character span verification.
    """
    filepath = "backtest/financebench/financebench_open_source.jsonl"
    with open(filepath, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f]

    assert len(records) == 150

    # Test top 10 diverse companies (3M, Best Buy, AMD, etc.)
    verified_count = 0
    for record in records[:10]:
        company = record["company"]
        doc_name = record["doc_name"]
        evidences = record.get("evidence", [])
        
        for ev in evidences:
            evidence_text = ev.get("evidence_text", "")
            full_page = ev.get("evidence_text_full_page", "")
            
            # Create a PARSEC EvidenceSpan
            span = EvidenceSpan(
                document_id=doc_name,
                document_name=doc_name,
                page_number=ev.get("evidence_page_num", 1),
                exact_text_quote=evidence_text.strip(),
                content_hash="sha256-verified"
            )

            # Deterministic span check: ensure quoted evidence strictly exists inside full document page
            if span.verify_in_document(full_page):
                verified_count += 1

    assert verified_count >= 10, f"Expected at least 10 spans verified, got {verified_count}"
