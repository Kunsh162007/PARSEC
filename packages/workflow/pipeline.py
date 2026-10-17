"""PARSEC diligence pipeline.

Everything in a result is either computed from the documents supplied or returned by a live service.
Where a service is unavailable the section is left empty and named in result["unavailable"]; no value is
ever substituted. Every evidence quote is verbatim document text with its character offsets and the SHA-256
of the document it came from.
"""
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from packages.compute.lib import AddBackItem, ComputeLib
from packages.evidence.extractor import DocumentExtractionWorker
from packages.evidence.tracker import DataRoomTracker
from packages.gateway.llm import GatewayUnavailable, LLMGateway
from packages.schemas.models import (
    EvidenceSpan, Finding, KillCriterion, KillCriterionStatus, SeverityLevel,
)
from packages.tavily.client import TavilyRealityEngine

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_DIR = ROOT / "config" / "playbooks"
THRESHOLDS = ROOT / "config" / "thresholds.yaml"

INJECTION_RE = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above) (?:instructions|prompts)"
    r"|disregard (?:all |the )?(?:above|previous|prior) (?:instructions|guidance|text)"
    r"|</?system[^>\n]{0,40}>|system_prompt_override"
    r"|you are instructed to (?:automatically )?sign off"
    r"|do not flag (?:any|this)",
    re.IGNORECASE,
)
CUSTOMER_LINE = re.compile(r"(?:Client\s*\d+\s*\(([^)]+)\)|([A-Za-z0-9][A-Za-z0-9 &.\-]+)):\s*\$?[0-9,]+")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _span(document_id: str, text: str, quote: str, document_name: Optional[str] = None) -> Optional[EvidenceSpan]:
    """An evidence span for text that really is in the document, or None."""
    at = text.find(quote) if quote else -1
    if at == -1:
        return None
    return EvidenceSpan(
        document_id=document_id, document_name=document_name or document_id, page_number=1,
        exact_text_quote=quote, char_start=at, char_end=at + len(quote), content_hash=_sha256(text),
    )


def _line_containing(text: str, *needles: str) -> str:
    for line in text.splitlines():
        if all(n.lower() in line.lower() for n in needles):
            return line.strip()
    return ""


STANDARD_EBITDA_LINES = re.compile(r"interest|tax|depreciation|amortization|amortisation", re.I)
UNIT_SCALE = {"thousand": Decimal("1000"), "million": Decimal("1000000"), "billion": Decimal("1000000000")}
MAX_RECONCILIATION_READS = 2  # each read is a paid model call; the CIM and one other candidate are enough


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _scale(units: str) -> Optional[Decimal]:
    for word, factor in UNIT_SCALE.items():
        if word in (units or "").lower():
            return factor
    return None


class DiligencePipeline:
    def __init__(
        self,
        gateway: Optional[Any] = None,
        tavily: Optional[Any] = None,
        sector: str = "saas",
    ):
        self.gateway = gateway if gateway is not None else LLMGateway()
        self.tavily = tavily if tavily is not None else TavilyRealityEngine()
        self.sector = sector
        self.playbook = self._load_playbook(sector)
        self.findings: List[Finding] = []
        self.kill_criteria: List[KillCriterion] = []

    # ------------------------------------------------------------------ EBITDA reconciliation (real layouts)
    async def _read_reconciliation(self, documents: Dict[str, str], cim_name: str, unavailable: List[str]) -> Optional[Dict[str, Any]]:
        """Read the documents' own Adjusted EBITDA reconciliation with the model, checked by arithmetic."""
        from packages.workflow.bridge import extract_bridge

        candidates = [n for n in [cim_name, *documents] if n and re.search(r"adjusted\s+ebitda", documents.get(n, ""), re.I)]
        if not candidates:
            return None
        if not getattr(self.gateway, "is_configured", False):
            unavailable.append("EBITDA reconciliation reading (no Nebius API key)")
            return None
        last: Optional[Dict[str, Any]] = None
        for name in list(dict.fromkeys(candidates))[:MAX_RECONCILIATION_READS]:
            out = await extract_bridge(self.gateway, documents[name])
            out["document"] = name
            last = out
            if out["status"] in ("foots", "does_not_foot"):
                return out
        return last

    def _reconciliation_findings(self, rec: Optional[Dict[str, Any]], documents: Dict[str, str], multiple: Decimal, add) -> None:
        if not rec or rec.get("status") not in ("foots", "does_not_foot"):
            reason = f" Reading the reconciliation: {rec.get('reason')}" if rec and rec.get("reason") else ""
            add("FINDING-BASE-EBITDA-MISSING", title="Base EBITDA could not be found in the documents",
                severity=SeverityLevel.MANAGEABLE, category="Completeness",
                description="No reported or unadjusted EBITDA figure was found, so no adjusted EBITDA or price impact "
                            f"was computed. Nothing was assumed in its place.{reason}")
            return
        name, text = rec["document"], documents[rec["document"]]
        units = rec.get("units") or "units as stated"

        def ev(line: Dict[str, Any]) -> Optional[EvidenceSpan]:
            # The bridge already located this exact row; re-searching would point a repeated row at its first copy.
            if not line.get("ok"):
                return None
            start, end = line["start"], line["end"]
            if not (0 <= start < end <= len(text)):
                return None
            return EvidenceSpan(
                document_id=name, document_name=name, page_number=1, exact_text_quote=text[start:end],
                char_start=start, char_end=end, content_hash=_sha256(text),
            )

        if rec["status"] == "does_not_foot":
            add("FINDING-EBITDA-RECON-UNRESOLVED", title="The Adjusted EBITDA reconciliation could not be made to add up",
                severity=SeverityLevel.MANAGEABLE, category="QoE",
                description=(f"Read for {rec.get('period') or 'the latest period'}: the starting line plus the adjustments give "
                             f"{rec['computed_total']} but the stated Adjusted EBITDA is {rec['total']['value']} ({units}). "
                             "This usually means the table was misread, not that the company is wrong. Check it by hand."),
                evidence=[e for e in (ev(rec["start"]), ev(rec["total"])) if e])
            return
        addbacks = [a for a in rec["adjustments"] if a["value"] > 0 and not STANDARD_EBITDA_LINES.search(a["label"])]
        add("FINDING-EBITDA-RECON", title=f"Adjusted EBITDA of {rec['total']['value']} ({units}) reconciles to {rec['start']['label']}",
            severity=SeverityLevel.MINOR, category="QoE",
            description=(f"Read from the document's own reconciliation for {rec.get('period') or 'the latest period'}. Every line was "
                         f"found in the document and the lines add up to the stated total. {len(rec['adjustments'])} adjustments."),
            evidence=[e for e in (ev(rec["start"]), ev(rec["total"])) if e])
        if not addbacks:
            return
        from packages.workflow.bridge import ledger_years, recurs_in_every_period

        recurring = [a for a in addbacks if recurs_in_every_period(a)]
        if recurring:
            shown = "; ".join(f"{a['label']} ({', '.join(str(v) for v in a['periods'])})" for a in recurring)
            factor = _scale(units)
            amount = sum((a["value"] for a in recurring), Decimal("0"))
            impact_r = float(amount * factor * multiple) if factor else 0.0
            add("FINDING-ADDBACK-RECURS", title=f"{len(recurring)} add-back(s) appear in every period shown",
                severity=SeverityLevel.PRICE_ADJUSTER, category="QoE",
                description=(f"These are added back to reach Adjusted EBITDA, yet the company's own table shows them in every "
                             f"period column: {shown} ({units}). A cost that recurs each period is usually part of running "
                             "the business. Ask why it should be excluded."
                             + (f" Treating them as recurring would lower value at {multiple}x by ${impact_r:,.0f}." if impact_r else "")),
                price_impact_usd=impact_r, evidence=[e for e in (ev(a) for a in recurring[:6]) if e])
        in_ledger = []
        for a in addbacks:
            found = ledger_years(a["label"], documents, skip=name)
            if len(found["years"]) >= 2:
                in_ledger.append((a, found))
        if in_ledger:
            listing = "; ".join(f"{a['label']} in {', '.join(f['years'])}" for a, f in in_ledger)
            spans = []
            for _a, f in in_ledger:
                for hit in f["lines"][:2]:
                    sp = _span(hit["document"], documents[hit["document"]], hit["line"])
                    if sp:
                        spans.append(sp)
            add("FINDING-ADDBACK-IN-LEDGER", title=f"{len(in_ledger)} add-back(s) also appear in the ledger in several years",
                severity=SeverityLevel.PRICE_ADJUSTER, category="QoE",
                description=(f"The accounts show costs matching these add-backs in more than one year: {listing}. "
                             "Check whether they are really one-off. Matching is by wording, so confirm each line is the same cost."),
                evidence=spans[:6])
        total_addbacks = sum((a["value"] for a in addbacks), Decimal("0"))
        factor = _scale(units)
        impact = float(total_addbacks * factor * multiple) if factor else None
        listing = "; ".join(f"{a['label']} {a['value']}" for a in addbacks)
        add("FINDING-ADDBACKS-TO-TEST", title=f"{len(addbacks)} add-backs to evidence before relying on Adjusted EBITDA",
            severity=SeverityLevel.PRICE_ADJUSTER if impact else SeverityLevel.MANAGEABLE, category="QoE",
            description=(f"Beyond interest, tax, depreciation and amortisation, Adjusted EBITDA adds back: {listing} ({units}), "
                         f"{total_addbacks} in total. Ask for evidence that each is genuinely non-recurring."
                         + (f" If none were accepted, value at {multiple}x would be ${impact:,.0f} lower." if impact else "")),
            price_impact_usd=impact or 0.0,
            evidence=[e for e in (ev(a) for a in addbacks[:6]) if e])

    # ------------------------------------------------------------------ configuration
    @staticmethod
    def _load_playbook(sector: str) -> Dict[str, Any]:
        path = PLAYBOOK_DIR / f"{sector}.yaml"
        if not path.is_file():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    @staticmethod
    def _thresholds() -> Dict[str, float]:
        data = yaml.safe_load(THRESHOLDS.read_text(encoding="utf-8")) if THRESHOLDS.is_file() else {}
        return (data or {}).get("materiality", {})

    # ------------------------------------------------------------------ routing
    @staticmethod
    def _route(documents: Dict[str, str]) -> Dict[str, Tuple[str, str]]:
        """Pick the CIM, the accounts and the customer table by what each document contains, not by filename."""
        def customer_rows(text: str) -> int:
            # A document only counts as a customer table if it talks about customers or clients at all.
            if not re.search(r"\b(?:customers?|clients?|ARR)\b", text, re.IGNORECASE):
                return 0
            return sum(1 for line in text.splitlines() if CUSTOMER_LINE.search(line))

        scored: Dict[str, List[Tuple[int, str]]] = {"cim": [], "accounts": [], "customers": []}
        for name, text in documents.items():
            low = name.lower()
            scored["cim"].append((
                (3 if "cim" in low or "memorandum" in low else 0)
                + (3 if re.search(r"adjusted ebitda", text, re.I) else 0)
                + (3 if re.search(r"base operating ebitda|unadjusted ebitda", text, re.I) else 0), name))
            scored["accounts"].append((
                (3 if "account" in low or "ledger" in low else 0)
                + (2 if re.search(r"\bTable\s+1\b", text) else 0)
                + min(6, len(re.findall(r"\b(?:FY\d{2}|20\d{2})\s+[A-Za-z]", text))), name))
            scored["customers"].append((
                (3 if "customer" in low or "arr" in low.split("_") else 0) + min(6, customer_rows(text)), name))
        routed: Dict[str, Tuple[str, str]] = {}
        for role, options in scored.items():
            score, name = max(options, key=lambda x: x[0])
            routed[role] = (name, documents[name]) if score > 0 else ("", "")
        return routed

    # ------------------------------------------------------------------ run
    async def run_pipeline(self, deal_data: Dict[str, Any]) -> Dict[str, Any]:
        documents: Dict[str, str] = {k: v for k, v in (deal_data.get("documents") or {}).items() if str(v).strip()}
        if not documents:
            raise ValueError("At least one non-empty document is required.")
        company = str(deal_data.get("company_name") or "").strip() or "Unnamed target"
        multiple = Decimal(str(deal_data.get("multiple") or self.playbook.get("default_multiple") or 10.0))
        thresholds = self._thresholds()
        critical_pct = Decimal(str(thresholds.get("customer_concentration_critical_pct", 0.35))) * 100
        warning_pct = Decimal(str(thresholds.get("customer_concentration_warning_pct", 0.20))) * 100

        self.findings = []
        unavailable: List[str] = []
        # What this run spends, so a caller can charge it to a budget even when the gateway is shared.
        cost_before = float(getattr(self.gateway, "total_cost_usd", 0.0) or 0.0)
        tavily_calls_before = int(getattr(self.tavily, "call_count", 0) or 0)

        def add(finding_id: str, **kwargs: Any) -> Finding:
            f = Finding(id=finding_id, **kwargs)
            self.findings.append(f)
            return f

        # Stage 2: living data room (hashing, versioning, gaps)
        tracker = DataRoomTracker(expected_schema=list(self.playbook.get("expected_documents", [])))
        for name, text in documents.items():
            _, tracker_findings = tracker.ingest_document(filename=name, content=text)
            self.findings.extend(tracker_findings)
        routed = self._route(documents)
        cim_name, cim_text = routed["cim"]
        acc_name, acc_text = routed["accounts"]
        cust_name, cust_text = routed["customers"]

        # Document integrity: text that tries to instruct the reviewer
        for name, text in documents.items():
            m = INJECTION_RE.search(text)
            if m:
                ev = _span(name, text, m.group(0))
                add("FINDING-SEC-1", title="Text in a document tries to instruct the reviewer",
                    severity=SeverityLevel.DEAL_BREAKER, category="Security",
                    description="A document contains wording aimed at an automated or human reviewer. It was not followed.",
                    evidence=[ev] if ev else [])
                break

        # Stage 3: quality of earnings
        bridge = None
        addbacks: List[AddBackItem] = []
        base = Decimal("0.00")
        if cim_text:
            base, _ = DocumentExtractionWorker.extract_base_ebitda(cim_text)
        reconciliation: Optional[Dict[str, Any]] = None
        if base == Decimal("0.00"):
            reconciliation = await self._read_reconciliation(documents, cim_name, unavailable)
            self._reconciliation_findings(reconciliation, documents, multiple, add)
        else:
            addbacks = DocumentExtractionWorker.extract_addback_candidates(cim_text, acc_text)
            bridge = ComputeLib.calculate_ebitda_bridge(unadjusted_ebitda=base, add_backs=addbacks, multiple=multiple)
            for ab in addbacks:
                if ab.is_accepted:
                    continue
                spans: List[EvidenceSpan] = []
                claim_line = _line_containing(cim_text, ab.description)
                for doc_id, text, quote in ((cim_name, cim_text, claim_line), (acc_name, acc_text, ab.evidence_span or "")):
                    sp = _span(doc_id, text, quote) if quote else None
                    if sp:
                        spans.append(sp)
                add(f"FINDING-QOE-{ab.id}", title=f"Disallowed add-back: {ab.description}",
                    severity=SeverityLevel.PRICE_ADJUSTER, category="QoE",
                    description=(f"Seller claimed ${float(ab.amount):,.2f} as non-recurring, but the ledger shows the "
                                 "same cost in more than one year."),
                    price_impact_usd=float(ab.amount * multiple), evidence=spans)

        # Customer concentration
        concentration = None
        customers = DocumentExtractionWorker.extract_customer_concentration(cust_text) if cust_text else []
        if customers:
            concentration = ComputeLib.calculate_customer_concentration(customers)
            named = [c for c in customers if not c.get("is_bucket")]
            top = max(named, key=lambda c: c["revenue"]) if named else None
            pct = concentration.top_customer_pct
            if top is not None and pct >= warning_pct:
                line = _line_containing(cust_text, top["name"])
                ev = _span(cust_name, cust_text, line) if line else None
                critical = pct >= critical_pct
                add("FINDING-CONC-1",
                    title=f"Customer concentration: {top['name']} is {float(pct):.0f}% of revenue",
                    severity=SeverityLevel.DEAL_BREAKER if critical else SeverityLevel.PRICE_ADJUSTER,
                    category="Concentration",
                    description=f"{top['name']} represents {float(pct):.1f}% of revenue in the customer table "
                                f"(warning at {float(warning_pct):.0f}%, critical at {float(critical_pct):.0f}%).",
                    evidence=[ev] if ev else [])

        # Outside-in check (live web only)
        tavily_state: Dict[str, Any] = {"api_calls": 0, "available": False, "reason": ""}
        try:
            web = await self.tavily.verify_external_claims(company, [])
        except Exception as exc:  # a failing service must not take the pipeline down
            web = None
            tavily_state["reason"] = type(exc).__name__
        # Tavily reports no price, so its spend is counted in requests made, including ones that failed.
        tavily_state["api_calls"] = int(getattr(self.tavily, "call_count", 0) or 0) - tavily_calls_before
        if web is not None and web.available:
            tavily_state["available"] = True
            for flag in web.flags:
                src = next((s for s in web.sources if s.get("title") and s["title"] in flag), None)
                if src is None:
                    continue
                snippet = src.get("snippet") or src.get("title", "")
                count = len([f for f in self.findings if f.category == "RealityCheck"]) + 1
                add(f"FINDING-WEB-{count}",
                    title=f"Public sources mention a dispute involving {company}",
                    severity=SeverityLevel.MANAGEABLE, category="RealityCheck",
                    description=f"{flag}. Confirm the source concerns this company before relying on it.",
                    evidence=[EvidenceSpan(document_id=src["url"], document_name=src.get("title", src["url"]),
                                           page_number=1, exact_text_quote=snippet, content_hash=_sha256(snippet))])
        else:
            unavailable.append("web_check")
            tavily_state["reason"] = tavily_state["reason"] or (web.reason if web is not None else "")

        # Kill criteria from the sector playbook, tested only against what the documents prove
        self.kill_criteria = []
        finding_ids = {f.id for f in self.findings}
        for template in self.playbook.get("kill_criteria_templates", []):
            kc = KillCriterion(id=template["id"], title=template["title"], condition=template["condition"])
            title = template["title"].lower()
            if "concentration" in title and concentration is not None:
                broken = "FINDING-CONC-1" in finding_ids
                kc.status = KillCriterionStatus.BROKEN if broken else KillCriterionStatus.HOLDING
                if broken:
                    kc.linked_findings.append("FINDING-CONC-1")
            elif ("add-back" in title or "add back" in title) and bridge is not None:
                rejected = [f"FINDING-QOE-{a.id}" for a in addbacks if not a.is_accepted]
                kc.status = KillCriterionStatus.BROKEN if rejected else KillCriterionStatus.HOLDING
                kc.linked_findings.extend(rejected)
            self.kill_criteria.append(kc)

        # Questions for the committee, from the real findings only
        ic: Dict[str, Any] = {}
        if self.findings:
            digest = [{"title": f.title, "category": f.category, "description": f.description,
                       "price_impact_usd": f.price_impact_usd,
                       "quote": f.evidence[0].exact_text_quote if f.evidence else ""} for f in self.findings[:12]]
            try:
                resp = await self.gateway.complete(
                    task="ic_simulator", use_cache=False,
                    messages=[
                        {"role": "system", "content": "You are a sceptical investment committee member. Using only "
                         "the findings supplied, write up to five hard questions the deal team must answer before "
                         "approval. Return JSON: {\"questions\": [\"...\"]}."},
                        {"role": "user", "content": json.dumps(digest)},
                    ],
                )
                ic = resp.parsed_json or {"questions": [resp.content.strip()]}
            except GatewayUnavailable:
                unavailable.append("llm")

        deal_hash = _sha256("".join(sorted(f"{n}:{_sha256(t)}" for n, t in documents.items())))[:12]
        hero = None
        if bridge is not None:
            hero = {
                "claimed_ebitda": float(bridge.seller_claimed_ebitda),
                "parsec_ebitda": float(bridge.parsec_adjudicated_ebitda),
                "delta_ebitda": float(bridge.delta_ebitda),
                "multiple": float(multiple),
                "potential_overpayment_avoided": float(bridge.price_impact_overpayment),
            }
        return {
            "deal_id": f"DEAL-{deal_hash}",
            "company_name": company,
            "sector": self.sector,
            "hero_metric": hero,
            "bridge": bridge.model_dump() if bridge is not None else None,
            "ebitda_reconciliation": _jsonable(reconciliation),
            "concentration": concentration.model_dump() if concentration is not None else None,
            "kill_criteria": [kc.model_dump() for kc in self.kill_criteria],
            "findings": [f.model_dump() for f in self.findings],
            "ic_grilling": ic,
            "data_room_gaps": tracker.detect_missing_gaps(),
            "unavailable": sorted(set(unavailable)),
            "tokens_meter": {
                "nano": self.gateway.total_tokens_nano,
                "super": self.gateway.total_tokens_super,
                "ultra": self.gateway.total_tokens_ultra,
                "total_cost_usd": self.gateway.total_cost_usd,
            },
            "tavily_meter": tavily_state,
            "cost_usd": round(max(0.0, float(self.gateway.total_cost_usd) - cost_before), 6),
        }
