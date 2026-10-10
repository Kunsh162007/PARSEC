import hashlib
import time
from typing import List, Dict, Any, Tuple
from packages.schemas.models import DataRoomDocument, Finding, SeverityLevel, EvidenceSpan

class DataRoomTracker:
    """
    Stage 2 Engine: Zero-Token Living Data Room Tracker.
    Performs content hashing, versioning, numeric diffs between re-uploads,
    behavioral telemetry (quiet updates, priority drift), and gap detection.
    Zero tokens spent.
    """

    def __init__(self, expected_schema: List[str]):
        self.expected_schema = expected_schema
        self.documents: Dict[str, List[DataRoomDocument]] = {} # filename -> list of versions
        self.audit_log: List[Dict[str, Any]] = []

    def ingest_document(self, filename: str, content: str, file_type: str = "text") -> Tuple[DataRoomDocument, List[Finding]]:
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        findings: List[Finding] = []

        now = time.time()
        if filename in self.documents:
            prev_versions = self.documents[filename]
            latest = prev_versions[-1]
            new_version_num = latest.version + 1

            quiet_reupload = False
            numeric_diff = False

            # Check if text/numbers changed without explicit notification
            if content_hash != latest.content_hash:
                quiet_reupload = True
                
                # Check for numeric differences
                import re
                old_numbers = set(re.findall(r"\$?\d+(?:,\d+)*(?:\.\d+)?", latest.content))
                new_numbers = set(re.findall(r"\$?\d+(?:,\d+)*(?:\.\d+)?", content))
                diff_numbers = new_numbers.symmetric_difference(old_numbers)
                if diff_numbers:
                    numeric_diff = True
                    findings.append(Finding(
                        id=f"FINDING-DR-{int(now)}",
                        title=f"Quiet Re-upload with Numeric Alterations in {filename}",
                        severity=SeverityLevel.PRICE_ADJUSTER,
                        category="DataRoomBehavior",
                        description=f"File {filename} was re-uploaded silently with {len(diff_numbers)} changed numeric values (e.g. {list(diff_numbers)[:3]}).",
                        evidence=[EvidenceSpan(
                            document_id=filename,
                            document_name=filename,
                            page_number=1,
                            exact_text_quote=f"Hash changed from {latest.content_hash[:8]} to {content_hash[:8]}",
                            content_hash=content_hash
                        )]
                    ))

            doc = DataRoomDocument(
                id=f"{filename}_v{new_version_num}",
                filename=filename,
                file_type=file_type,
                upload_timestamp=now,
                content=content,
                content_hash=content_hash,
                version=new_version_num,
                previous_version_hash=latest.content_hash,
                quiet_reupload_flag=quiet_reupload,
                numeric_diff_detected=numeric_diff
            )
            prev_versions.append(doc)
        else:
            doc = DataRoomDocument(
                id=f"{filename}_v1",
                filename=filename,
                file_type=file_type,
                upload_timestamp=now,
                content=content,
                content_hash=content_hash,
                version=1
            )
            self.documents[filename] = [doc]

        return doc, findings

    def detect_missing_gaps(self) -> List[str]:
        uploaded_files = set(self.documents.keys())
        missing = []
        for expected in self.expected_schema:
            matched = any(expected.lower() in fname.lower() or fname.lower() in expected.lower() for fname in uploaded_files)
            if not matched:
                missing.append(expected)
        return missing

    def generate_supplementary_request_list(self) -> str:
        gaps = self.detect_missing_gaps()
        if not gaps:
            return "All expected data room items have been received."
        
        lines = ["# SUPPLEMENTARY DILIGENCE REQUEST LIST (STAGE 2 GAPS)", ""]
        for idx, item in enumerate(gaps, 1):
            lines.append(f"{idx}. {item} - [CRITICAL: Pending initial receipt for Quality of Earnings verification]")
        return "\n".join(lines)
