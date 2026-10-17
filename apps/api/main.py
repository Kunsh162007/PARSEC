from fastapi import FastAPI, Header, HTTPException, Path as PathParam, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator
from typing import Annotated, Dict, Any, List, Mapping, Optional, Sequence, Tuple
from collections import OrderedDict
from contextlib import asynccontextmanager
import contextlib
import datetime
import ipaddress
import json
import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from packages.gateway.llm import LLMGateway
from packages.review.deep import (
    LENS_IDS,
    TASK as DEEP_REVIEW_TASK,
    TASK_ADJUDICATE,
    TASK_TRIAGE,
    DeepReviewUnavailable,
    deep_review,
)
from packages.review import rules as review_rules
from packages.review.assistant import interpret as interpret_command
from apps.api import deals_routes
from packages.common.envfile import load_env_defaults
from packages.review.clauses import clause_findings
from packages.review.guard import Reservation, ReviewGuard
from packages.review.memo import draft_memo
from packages.review.requirements import determine_requirements
from packages.review.web import web_check

# Make keys in the git-ignored .env visible to every module that reads os.environ (Tavily, review guard).
load_env_defaults()
from packages.workflow.pipeline import DiligencePipeline

logger = logging.getLogger("parsec.api")
DEFAULT_BIND_HOST = "127.0.0.1"  # uvicorn's default when --host is not given


def _bind_host(argv: Sequence[str], env: Mapping[str, str]) -> str:
    """The host uvicorn was told to listen on, read from its command line or UVICORN_HOST."""
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--host="):
            return arg.split("=", 1)[1]
    return env.get("UVICORN_HOST") or DEFAULT_BIND_HOST


def _is_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def _open_access_warning(host: str, auth_required: bool) -> str:
    """A startup warning when anyone on the network could spend the API keys, or "" when that is not so."""
    if auth_required or _is_loopback(host):
        return ""
    return (f"REVIEW_API_TOKEN is not set and the server listens on {host}, not only on this machine. The paid "
            "endpoints (/api/v1/diligence/* with ai or web, /api/v1/data-room/*, /api/v1/companies/lookup and their "
            "older /api/review/*, /api/deals/* paths) are open to anyone who can reach it and "
            "the deal folders on this machine (/api/v1/deals/*) can be read and written by them, and "
            "will spend the Nebius and Tavily keys up to REVIEW_DAILY_BUDGET_USD per day. Set REVIEW_API_TOKEN, "
            "or bind to 127.0.0.1.")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    message = _open_access_warning(_bind_host(sys.argv, os.environ), review_guard.auth_required)
    if message:
        logger.warning(message)
    yield


app = FastAPI(
    title="PARSEC M&A Due Diligence API",
    description=(
        "Everything sits under one parent endpoint, **GET /api/v1**, which lists its children: one due diligence "
        "endpoint per category (`/api/v1/diligence/{category}`), the data room (`/api/v1/data-room/*`), company "
        "lookups and measured benchmarks. The older `/api/review/*` and `/api/deals/*` paths still answer and are "
        "marked deprecated."
    ),
    version="3.1.0",
    lifespan=_lifespan,
)

# Browser hardening for the bundled pages. pdf.js and mammoth load from cdnjs; pdf.js runs its worker from a
# blob: wrapper around the cdnjs script. Fonts come from Google Fonts; favicons are data: URIs.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    # wasm-unsafe-eval lets the self-hosted OCR engine compile its WebAssembly; it does not allow eval().
    "script-src 'self' 'wasm-unsafe-eval' https://cdnjs.cloudflare.com",
    "worker-src 'self' blob: https://cdnjs.cloudflare.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob:",
    "connect-src 'self' https://cdnjs.cloudflare.com",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
])
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if not request.url.path.startswith("/api/"):
        # Pages and scripts are revalidated (cheaply, by ETag) so a browser never mixes an old engine.js with a
        # new app.js after an update.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response

# Host names this server answers to. A page on another site that points its own domain at 127.0.0.1 (DNS
# rebinding) still sends its own name, so it is refused before it can read a deal folder. Add names with
# ALLOWED_HOSTS=a,b; "testserver" is the name the test client uses.
_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost,[::1],testserver").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_HOSTS)

# CORS: the bundled pages are served from this same origin. Add others with CORS_ORIGINS=a,b (comma separated).
_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Review-Token"],
)

# Fine Print: general-purpose document checker. Static, runs entirely in the browser.
APPS_DIR = Path(__file__).resolve().parents[1]
for _route, _folder in (("/review", "review"), ("/parsec", "web")):
    if (APPS_DIR / _folder).is_dir():
        app.mount(_route, StaticFiles(directory=APPS_DIR / _folder, html=True), name=_folder)


class ReviewDocument(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., max_length=200_000)


class DeepReviewRequest(BaseModel):
    documents: List[ReviewDocument] = Field(..., min_length=1, max_length=10)
    lenses: List[str] = Field(default_factory=list, max_length=12)
    ai: bool = True            # Super reads the documents; every finding must quote them
    web: bool = False          # Tavily outside-in check of the companies named in the documents
    # The extra tiers are off by default because benchmarks/review found no measurable gain from them:
    # Nano triage skipped nothing on a long document, and Ultra rulings cost ~3.7x for no change in accuracy.
    triage: bool = False       # Nano decides which parts of long documents are worth reading
    adjudicate: bool = False   # Ultra rules on the critical and high findings


review_guard = ReviewGuard.from_env()


def _tavily_live() -> bool:
    return bool(os.getenv("TAVILY_API_KEY", "").strip())


@app.get("/api/review/status")
def review_status():
    """Tells the Fine Print page which optional reads it can offer, and what protects them."""
    gateway = LLMGateway()
    return {
        "live": gateway.is_configured,
        "model": gateway.route_task(DEEP_REVIEW_TASK)["model"],
        "tiers": {
            "nano": gateway.route_task(TASK_TRIAGE)["model"],
            "super": gateway.route_task(DEEP_REVIEW_TASK)["model"],
            "ultra": gateway.route_task(TASK_ADJUDICATE)["model"],
        },
        "web": _tavily_live(),
        **review_guard.status(),
    }


def _check_rate(request: Request) -> None:
    """Per-client rate limit, for every endpoint that does real work (paid or not)."""
    allowed, wait = review_guard.rate_ok(request.client.host if request.client else "unknown")
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Too many requests. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})


def _check_access(request: Request, token: Optional[str]) -> None:
    """Rate limit and access code, for every endpoint that spends money or exposes a stored deal."""
    _check_rate(request)
    if not review_guard.token_ok(token):
        raise HTTPException(status_code=401, detail="An access code is required, or the one given is wrong.")


def _enforce_review_guard(request: Request, token: Optional[str]) -> Reservation:
    """Rate limit, access code and daily budget shared by every paid endpoint.

    Returns a budget reservation: use it as a context manager around the paid work and record() the real cost,
    so concurrent requests cannot all pass the budget check before any of them is charged."""
    _check_access(request, token)
    reservation = review_guard.reserve()
    if reservation is None:
        raise HTTPException(status_code=429, detail="The daily AI budget for this server has been used. Try again tomorrow.")
    return reservation


class RequirementsRequest(BaseModel):
    documents: List[ReviewDocument] = Field(..., min_length=1, max_length=12)


@app.post("/api/review/requirements")
async def review_requirements(
    req: RequirementsRequest,
    request: Request,
    x_review_token: Optional[str] = Header(default=None),
):
    """What the supplied file still needs, judged by a reasoning model. No fixed checklist is ever substituted."""
    with _enforce_review_guard(request, x_review_token) as spend:
        out = await determine_requirements(LLMGateway(), [d.model_dump() for d in req.documents])
        spend.record(out.get("cost_usd", 0.0))
    return out


class MemoFinding(BaseModel):
    severity: str = Field("", max_length=20)
    title: str = Field("", max_length=300)
    where: str = Field("", max_length=600)
    quote: str = Field("", max_length=1500)
    detail: str = Field("", max_length=1000)


class MemoNeed(BaseModel):
    item: str = Field(..., min_length=1, max_length=400)
    why: str = Field("", max_length=800)


class MemoRequest(BaseModel):
    findings: List[MemoFinding] = Field(default_factory=list, max_length=40)
    needs: List[MemoNeed] = Field(default_factory=list, max_length=20)
    engagement: str = Field("", max_length=600)


@app.post("/api/review/memo")
async def review_memo(
    req: MemoRequest,
    request: Request,
    x_review_token: Optional[str] = Header(default=None),
):
    """A short memo drafted by Nemotron from verified findings. Uncited points and unsupported numbers are removed."""
    with _enforce_review_guard(request, x_review_token) as spend:
        out = await draft_memo(LLMGateway(), [f.model_dump() for f in req.findings], [n.model_dump() for n in req.needs], req.engagement)
        spend.record(out.get("cost_usd", 0.0))
    out.pop("evidence", None)
    return out


@app.post("/api/review/deep")
async def review_deep(
    req: DeepReviewRequest,
    request: Request,
    x_review_token: Optional[str] = Header(default=None),
):
    """Funnel review plus optional web check. Only findings quoted from the documents are returned."""
    if not (req.ai or req.web):
        _check_access(request, x_review_token)
        raise HTTPException(status_code=422, detail="Turn on the AI read, the web check, or both.")

    documents = [d.model_dump() for d in req.documents]
    out: Dict[str, Any] = {"web": None}
    with _enforce_review_guard(request, x_review_token) as spend:
        if req.ai:
            want_clauses = not req.lenses or "legal" in req.lenses
            ai_out, unavailable = await _ai_read(documents, req.lenses, spend, want_clauses=want_clauses,
                                                 triage=req.triage, adjudicate=req.adjudicate)
            if unavailable and not (req.web or ai_out["findings"]):
                raise HTTPException(status_code=503, detail=unavailable)
            out.update(ai_out)
        else:
            out.update({"findings": [], "ai_error": "", "clause_error": "", "clauses": None})
        if req.web:
            out["web"] = await web_check(documents, categories=req.lenses)
    return out


async def _ai_read(
    documents: List[Dict[str, str]],
    lenses: List[str],
    spend: Reservation,
    *,
    want_clauses: bool,
    triage: bool = False,
    adjudicate: bool = False,
) -> Tuple[Dict[str, Any], str]:
    """The Nemotron read plus, when wanted, the clause pass. Returns the result and, if the model read could not
    run, why (""). Costs are recorded on the caller's budget reservation as they are known."""
    out: Dict[str, Any] = {"findings": [], "ai_error": "", "clause_error": "", "clauses": None}
    gateway = LLMGateway()
    deep_task = deep_review(gateway, documents, lenses, use_triage=triage, use_adjudication=adjudicate)
    clause_task = clause_findings(gateway, documents) if want_clauses else asyncio.sleep(0, result=None)
    deep_out, clause_out = await asyncio.gather(deep_task, clause_task, return_exceptions=True)
    if isinstance(clause_out, dict):
        out["clauses"] = clause_out["usage"]
        spend.record(clause_out["usage"]["cost_usd"])
    elif isinstance(clause_out, Exception):
        # Reported, never silently dropped: the user must know the clause check did not run.
        logger.warning("Clause check failed", exc_info=clause_out)
        out["clause_error"] = f"The clause check failed: {str(clause_out) or type(clause_out).__name__}"
    elif isinstance(clause_out, BaseException):
        raise clause_out
    unavailable = ""
    if isinstance(deep_out, DeepReviewUnavailable):
        unavailable = str(deep_out)
        out["ai_error"] = unavailable
    elif isinstance(deep_out, BaseException):
        raise deep_out
    else:
        out.update(deep_out)
        spend.record(deep_out.get("cost_usd", 0.0))
    if isinstance(clause_out, dict):
        out["findings"] = [*out.get("findings", []), *clause_out["findings"]]
    return out, unavailable

# In-memory storage for deals, bounded so repeated runs cannot exhaust memory; the oldest deal is evicted first.
MAX_STORED_DEALS = 100
MAX_DEAL_DOCUMENTS = 10
MAX_NAME_CHARS = 200
MAX_DOCUMENT_CHARS = 200_000
deals_store: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _store_deal(deal: Dict[str, Any]) -> None:
    deals_store[deal["deal_id"]] = deal
    while len(deals_store) > MAX_STORED_DEALS:
        deals_store.popitem(last=False)


class DealRunRequest(BaseModel):
    company_name: str = Field("Unnamed target", min_length=1, max_length=MAX_NAME_CHARS)
    multiple: float = Field(10.0, ge=1.0, le=50.0)
    sector: str = Field("saas", pattern=r"^[a-z_]{1,32}$")
    documents: Optional[Dict[str, str]] = None

    @field_validator("documents")
    @classmethod
    def _bounded_documents(cls, value: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        if value is None:
            return value
        if len(value) > MAX_DEAL_DOCUMENTS:
            raise ValueError(f"At most {MAX_DEAL_DOCUMENTS} documents per run.")
        for name, text in value.items():
            if not 1 <= len(name) <= MAX_NAME_CHARS:
                raise ValueError(f"Document names must be 1 to {MAX_NAME_CHARS} characters.")
            if len(text) > MAX_DOCUMENT_CHARS:
                raise ValueError(f"Each document may hold at most {MAX_DOCUMENT_CHARS:,} characters.")
        return value

@app.get("/health")
def health_check():
    """Liveness plus what is actually configured, read from the environment and config, not asserted."""
    gateway = LLMGateway()
    return {
        "status": "ok",
        "service": "parsec-api",
        "nebius_configured": gateway.is_configured,
        "tavily_configured": _tavily_live(),
        "models": {
            "nano": gateway.route_task(TASK_TRIAGE)["model"],
            "super": gateway.route_task(DEEP_REVIEW_TASK)["model"],
            "ultra": gateway.route_task(TASK_ADJUDICATE)["model"],
        },
    }

@app.post("/api/deals/run")
async def run_acquisition_deal(
    req: DealRunRequest,
    request: Request,
    x_review_token: Optional[str] = Header(default=None),
):
    """Runs the diligence pipeline on documents the caller supplies. There is no built-in sample deal:
    without documents there is nothing to analyse."""
    documents = {k: v for k, v in (req.documents or {}).items() if str(v).strip()}
    if not documents:
        raise HTTPException(status_code=422, detail="Provide at least one document: {\"documents\": {\"name.txt\": \"text\"}}.")
    with _enforce_review_guard(request, x_review_token) as spend:
        result = await DiligencePipeline(sector=req.sector).run_pipeline({
            "company_name": req.company_name,
            "multiple": req.multiple,
            "documents": documents,
        })
        spend.record(result.get("cost_usd", 0.0))
    # The id must not be guessable from the documents; the content hash is kept as its own field.
    result = {**result, "document_hash": result.get("deal_id"), "deal_id": f"DEAL-{secrets.token_urlsafe(16)}"}
    _store_deal(result)
    return result

@app.get("/api/companies/lookup")
async def lookup_company(
    request: Request,
    company_name: str = Query(..., min_length=1, max_length=MAX_NAME_CHARS),
    x_review_token: Optional[str] = Header(default=None),
):
    """Live public-source check on a named company (Tavily). Reports unavailable rather than guessing."""
    from packages.tavily.client import TavilyRealityEngine
    with _enforce_review_guard(request, x_review_token):
        result = await TavilyRealityEngine().verify_external_claims(company_name, [])
    return {
        "company_name": company_name,
        "available": result.available,
        "reason": result.reason,
        "claims_verified": result.claims_verified,
        "risk_flags": result.flags,
        "sources": result.sources,
    }

@app.get("/api/deals/{deal_id}")
def get_deal(deal_id: str, request: Request, x_review_token: Optional[str] = Header(default=None)):
    _check_access(request, x_review_token)
    if deal_id not in deals_store:
        raise HTTPException(status_code=404, detail="Deal not found")
    return deals_store[deal_id]

@app.post("/api/deals/{deal_id}/findings/{finding_id}/sign")
def sign_finding(deal_id: str, finding_id: str, request: Request, x_review_token: Optional[str] = Header(default=None)):
    _check_access(request, x_review_token)
    deal = deals_store.get(deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Deal not found")
    for f in deal["findings"]:
        if f["id"] == finding_id:
            f["signed_by_analyst"] = True
            return {"status": "signed", "finding": f}
    raise HTTPException(status_code=404, detail="Finding not found")

BENCHMARKS_DIR = Path(__file__).resolve().parents[2] / "benchmarks"


def _measured_results(relative: str, command: str) -> Dict[str, Any]:
    """Serve a results file written by a benchmark runner. Nothing is hardcoded here: if the file is
    missing the honest answer is "not run", with the command that produces it."""
    path = BENCHMARKS_DIR / relative
    if not path.is_file():
        return {"status": "not_run", "detail": f"No results yet. Run: {command}"}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/arena/benchmark")
def get_arena_benchmark():
    """Fine Print against CUAD: 510 real contracts with lawyer-labeled clauses."""
    return _measured_results("cuad/results.json", "python -m benchmarks.cuad.dataset --fetch && python -m benchmarks.cuad.run")


@app.get("/api/arena/web-evidence")
def get_web_evidence():
    """Live Tavily lookups on real and invented companies, with the source URLs and retrieval time."""
    return _measured_results("review/web_live.json", "python -m benchmarks.review.web_live")


@app.get("/api/arena/parsec")
def get_parsec_benchmark():
    """PARSEC reading real Adjusted EBITDA reconciliations from SEC 8-K earnings releases, held-out half."""
    return _measured_results("ebitda/results_test.json",
                             "python -m benchmarks.ebitda.fetch && python -m benchmarks.ebitda.run --split test")


# ---------------------------------------------------------------------------------------------------------------
# /api/v1: one parent endpoint. Every part of the project (due diligence by category, the data room, company
# lookups, benchmarks) is a child of it, and GET /api/v1 lists them all.
# ---------------------------------------------------------------------------------------------------------------
V1 = "/api/v1"
OFFSETS_NOTE = ("Span offsets are Unicode code points into each document's text after line endings are "
                "normalised to \\n; spans[].text is the flagged passage.")
SEVERITY_ORDER = ("critical", "high", "medium", "low")


class DiligenceRequest(BaseModel):
    documents: List[ReviewDocument] = Field(..., min_length=1, max_length=10)
    ai: bool = False     # add the Nemotron read for these categories (paid; needs the access code when one is set)
    web: bool = False    # Tavily public-source check of the companies named (paid)
    as_of: Optional[datetime.date] = None  # "today" for the date checks; the server's date when left out


class CombinedDiligenceRequest(DiligenceRequest):
    categories: List[str] = Field(default_factory=list, max_length=len(LENS_IDS))  # empty: every category

    @field_validator("categories")
    @classmethod
    def _known_categories(cls, value: List[str]) -> List[str]:
        unknown = [c for c in value if c not in LENS_IDS]
        if unknown:
            raise ValueError(f"Unknown categories: {', '.join(unknown)}. Valid: {', '.join(sorted(LENS_IDS))}.")
        return list(dict.fromkeys(value))


def _category_list() -> List[Dict[str, str]]:
    try:
        cats = review_rules.categories()
    except review_rules.RulesUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return [{**c, "path": f"{V1}/diligence/{c['id']}"} for c in cats]


def _with_span_text(finding: Dict[str, Any], documents: List[Dict[str, str]]) -> Dict[str, Any]:
    spans = [
        {**s, "text": documents[s["doc"]]["text"][s["start"]:s["end"]]} if 0 <= s.get("doc", -1) < len(documents)
        else dict(s)
        for s in finding.get("spans", [])
    ]
    return {**finding, "spans": spans}


def _located(finding: Dict[str, Any], documents: List[Dict[str, str]]) -> Dict[str, Any]:
    """Name the documents a finding is in; more than one means the problem is between documents."""
    indexes = list(dict.fromkeys(s["doc"] for s in finding.get("spans", []) if 0 <= s.get("doc", -1) < len(documents)))
    return {**finding, "documents": [documents[i]["name"] for i in indexes], "cross_document": len(indexes) > 1}


def _verdict(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {s: sum(1 for f in findings if f.get("severity") == s) for s in SEVERITY_ORDER}
    return {"level": next((s for s in SEVERITY_ORDER if counts[s]), "none"), "counts": counts}


def _rate_only(request: Request) -> contextlib.nullcontext:
    _check_rate(request)
    return contextlib.nullcontext()


async def _diligence(categories: List[str], req: DiligenceRequest, request: Request,
                     token: Optional[str]) -> Dict[str, Any]:
    """Rules for the chosen categories (free), plus the AI read and web check when asked (paid, guarded)."""
    documents = [{"name": d.name, "text": review_rules.normalize_text(d.text)} for d in req.documents]
    selected = [c for c in categories if c != "integrity"]
    wanted = set(categories) or {"integrity"}
    guard = _enforce_review_guard(request, token) if (req.ai or req.web) else _rate_only(request)
    ai: Dict[str, Any] = {"requested": req.ai, "ran": False, "error": "", "clause_error": "", "cost_usd": 0.0,
                          "dropped": 0}
    with guard as spend:
        try:
            rules_out = await review_rules.run_rules(documents, selected, req.as_of.isoformat() if req.as_of else None)
        except review_rules.RulesUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        found = list(rules_out["findings"])
        if req.ai:
            # An empty lens list means "all areas" to the model: an integrity-only check must say so explicitly.
            ai_out, unavailable = await _ai_read(documents, selected or ["integrity"], spend,
                                                 want_clauses="legal" in selected)
            clause_cost = (ai_out.get("clauses") or {}).get("cost_usd", 0.0)
            ai.update(ran=not unavailable, error=unavailable, clause_error=ai_out["clause_error"],
                      cost_usd=round(ai_out.get("cost_usd", 0.0) + clause_cost, 6), dropped=ai_out.get("dropped", 0))
            found += [_with_span_text(f, documents) for f in ai_out["findings"]]
        web = await web_check(documents, categories=selected) if req.web else None
        if web:
            found += [_with_span_text(f, documents) for f in web.get("findings", [])]
            web = {k: v for k, v in web.items() if k != "findings"}   # they are in the lists below
    found = [_located(f, documents) for f in found]
    found.sort(key=lambda f: SEVERITY_ORDER.index(f["severity"]) if f.get("severity") in SEVERITY_ORDER else 4)
    findings = [f for f in found if f.get("lens") in wanted]
    integrity = [f for f in found if f.get("lens") == "integrity" and "integrity" not in wanted]
    # The model's focus is advisory: points it raised in other areas were paid for, so they are returned, apart.
    other = [f for f in found if f.get("lens") not in wanted and f.get("lens") != "integrity"]
    return {
        "verdict": _verdict(findings + integrity),
        "findings": findings,
        "integrity": integrity,
        "other_categories": other,
        "ai": ai,
        "web": web,
        "stats": rules_out["stats"],
        "offsets": OFFSETS_NOTE,
    }


def diligence_categories():
    """Every due diligence category, each with its own endpoint."""
    return {"categories": _category_list()}


async def diligence_category(
    req: DiligenceRequest,
    request: Request,
    category: str = PathParam(..., max_length=20, description="A category id from /api/v1/diligence/categories"),
    x_review_token: Optional[str] = Header(default=None),
):
    """Due diligence for one category. The rules are free; `ai` and `web` add the paid reads for this category only.
    `findings` holds this category's points; `integrity` holds the always-on document checks."""
    if category not in LENS_IDS:
        raise HTTPException(status_code=404, detail=(
            f"No due diligence category '{category}'. Valid categories: {', '.join(sorted(LENS_IDS))}."))
    info = next((c for c in _category_list() if c["id"] == category), {"id": category})
    return {"category": info, **await _diligence([category], req, request, x_review_token)}


async def diligence_combined(
    req: CombinedDiligenceRequest,
    request: Request,
    x_review_token: Optional[str] = Header(default=None),
):
    """Due diligence for several categories in one call (all of them when `categories` is empty)."""
    chosen = req.categories or [c["id"] for c in _category_list()]
    return {"categories": chosen, **await _diligence(chosen, req, request, x_review_token)}


class AssistantFinding(BaseModel):
    n: int = Field(..., ge=1, le=1000)
    title: str = Field("", max_length=300)
    severity: str = Field("", max_length=20)
    documents: List[Annotated[str, Field(max_length=400)]] = Field(default_factory=list, max_length=10)
    why: str = Field("", max_length=400)


ShortName = Annotated[str, Field(max_length=400)]


class AssistantRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    deals: List[ShortName] = Field(default_factory=list, max_length=200)
    documents: List[ShortName] = Field(default_factory=list, max_length=200)
    categories: List[Annotated[str, Field(max_length=20)]] = Field(default_factory=list, max_length=12)
    findings: List[AssistantFinding] = Field(default_factory=list, max_length=60)


async def assistant_interpret(
    req: AssistantRequest,
    request: Request,
    x_review_token: Optional[str] = Header(default=None),
):
    """What a person said, as one checked command, or an answer that cites the problems found. Nemotron reads it;
    code keeps only listed actions, real folder and document names, and answers resting on real findings."""
    with _enforce_review_guard(request, x_review_token) as spend:
        out = await interpret_command(LLMGateway(), req.text, req.model_dump(exclude={"text"}))
        spend.record(out.get("cost_usd", 0.0))
    return out


def api_index():
    """The parent endpoint: every child endpoint of the project, what it does, and whether it can spend money."""
    return {
        "name": app.title,
        "version": app.version,
        "home": "/",
        "docs": "/docs",
        "endpoints": [
            {"method": method, "path": path, "group": group, "summary": summary, "paid": paid}
            for method, path, _handler, group, summary, paid in V1_ROUTES
        ],
    }


# (method, path, handler, group, summary, paid). Specific paths come before /diligence/{category}.
V1_ROUTES = [
    ("GET", V1, api_index, "Overview", "List every endpoint of the project", False),
    ("GET", f"{V1}/status", review_status, "Overview", "Which optional reads this server can offer", False),
    ("GET", f"{V1}/health", health_check, "Overview", "Liveness and what is configured", False),
    ("GET", f"{V1}/diligence/categories", diligence_categories, "Due diligence", "List the categories", False),
    ("POST", f"{V1}/diligence", diligence_combined, "Due diligence", "Check several categories at once", False),
    ("POST", f"{V1}/diligence/requirements", review_requirements, "Due diligence",
     "What the file still needs (Nemotron)", True),
    ("POST", f"{V1}/diligence/memo", review_memo, "Due diligence", "Draft a cited memo (Nemotron)", True),
    ("POST", f"{V1}/diligence/deep", review_deep, "Due diligence", "AI read across chosen categories", True),
    ("POST", f"{V1}/diligence/{{category}}", diligence_category, "Due diligence",
     "Check one category (rules free; ai/web paid)", False),
    ("POST", f"{V1}/data-room/run", run_acquisition_deal, "Data room", "Analyse a data room (PARSEC)", True),
    ("GET", f"{V1}/data-room/{{deal_id}}", get_deal, "Data room", "Fetch a stored data room analysis", False),
    ("POST", f"{V1}/data-room/{{deal_id}}/findings/{{finding_id}}/sign", sign_finding, "Data room",
     "Sign off a finding", False),
    ("GET", f"{V1}/companies/lookup", lookup_company, "Companies", "Public-source check on a company", True),
    ("GET", f"{V1}/benchmarks/cuad", get_arena_benchmark, "Benchmarks", "Rules on 510 CUAD contracts", False),
    ("GET", f"{V1}/benchmarks/web-evidence", get_web_evidence, "Benchmarks", "Live Tavily lookups", False),
    ("GET", f"{V1}/benchmarks/parsec", get_parsec_benchmark, "Benchmarks", "PARSEC on real earnings releases", False),
    *[(m, f"{V1}{p}", h, "Deal folders", s, False) for m, p, h, s in deals_routes.ROUTES],
    ("POST", f"{V1}/assistant/interpret", assistant_interpret, "Assistant", "Understand a free-form request", True),
]
deals_routes.token_check = lambda token: review_guard.token_ok(token)   # read at call time, so tests can swap it

# The paths used before /api/v1 keep answering, so nothing that calls them breaks; the docs mark them deprecated.
LEGACY_PREFIXES = ("/api/review/", "/api/deals/", "/api/companies/", "/api/arena/")
for _route in app.routes:
    if isinstance(_route, APIRoute) and _route.path.startswith(LEGACY_PREFIXES):
        _route.deprecated = True
        _route.tags = ["Older paths (deprecated)"]

for _method, _path, _handler, _group, _summary, _paid in V1_ROUTES:
    app.add_api_route(_path, _handler, methods=[_method], tags=[_group], summary=_summary)


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def api_not_found(rest: str):
    """Unknown API paths answer in JSON, rather than falling through to the home page's static files."""
    raise HTTPException(status_code=404, detail=f"No endpoint /api/{rest[:200]}. GET /api/v1 lists them all.")

# The home page, last so that it never shadows an API route.
if (APPS_DIR / "home").is_dir():
    app.mount("/", StaticFiles(directory=APPS_DIR / "home", html=True), name="home")
