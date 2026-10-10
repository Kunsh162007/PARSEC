from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from enum import Enum
import hashlib
import time

class SeverityLevel(str, Enum):
    DEAL_BREAKER = "deal_breaker"
    PRICE_ADJUSTER = "price_adjuster"
    MANAGEABLE = "manageable"
    MINOR = "minor"

class KillCriterionStatus(str, Enum):
    HOLDING = "holding"
    AT_RISK = "at_risk"
    BROKEN = "broken"

class KillCriterion(BaseModel):
    id: str
    title: str
    condition: str
    status: KillCriterionStatus = KillCriterionStatus.HOLDING
    linked_findings: List[str] = Field(default_factory=list)
    rationale: Optional[str] = None

class EvidenceSpan(BaseModel):
    document_id: str
    document_name: str
    page_number: int
    exact_text_quote: str
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    content_hash: str

    def verify_in_document(self, document_content: str) -> bool:
        """Deterministic span verification."""
        return self.exact_text_quote in document_content

class Finding(BaseModel):
    id: str
    title: str
    severity: SeverityLevel
    category: str  # QoE, Concentration, Footing, RealityCheck, DataRoomBehavior
    description: str
    price_impact_usd: float = 0.0
    kill_criteria_impacted: List[str] = Field(default_factory=list)
    evidence: List[EvidenceSpan] = Field(default_factory=list)
    signed_by_analyst: bool = False
    dismissed: bool = False
    analyst_notes: Optional[str] = None

class DataRoomDocument(BaseModel):
    id: str
    filename: str
    file_type: str
    upload_timestamp: float = Field(default_factory=time.time)
    content: str
    content_hash: str
    version: int = 1
    previous_version_hash: Optional[str] = None
    quiet_reupload_flag: bool = False
    numeric_diff_detected: bool = False

class AuditEvent(BaseModel):
    event_id: str
    timestamp: float = Field(default_factory=time.time)
    stage: str
    action: str
    details: Dict[str, Any]
    prev_event_hash: str
    current_hash: str = ""

    def compute_hash(self) -> str:
        raw = f"{self.event_id}:{self.timestamp}:{self.stage}:{self.action}:{self.prev_event_hash}"
        return hashlib.sha256(raw.encode()).hexdigest()

class DealWorkspace(BaseModel):
    deal_id: str
    company_name: str
    sector: str
    target_ebitda_claimed: float
    deal_multiple: float
    kill_criteria: List[KillCriterion] = Field(default_factory=list)
    documents: List[DataRoomDocument] = Field(default_factory=list)
    findings: List[Finding] = Field(default_factory=list)
    token_usage_nano: int = 0
    token_usage_super: int = 0
    token_usage_ultra: int = 0
    tavily_api_calls: int = 0
    estimated_cost_usd: float = 0.0
