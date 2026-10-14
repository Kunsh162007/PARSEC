from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from typing import Dict, Any, List, Mapping, Optional, Sequence
from collections import OrderedDict
from contextlib import asynccontextmanager
import ipaddress
import json
import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from packages.gateway.llm import LLMGateway
from packages.review.deep import (
    TASK as DEEP_REVIEW_TASK,
    TASK_ADJUDICATE,
    TASK_TRIAGE,
    DeepReviewUnavailable,
    deep_review,
)
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
            "endpoints (/api/review/*, /api/deals/*, /api/companies/lookup) are open to anyone who can reach it and "
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
    description="The due-diligence co-pilot that spends compute the way a senior analyst spends attention",
    version="3.0.0",
    lifespan=_lifespan,
)

# Browser hardening for the bundled pages. pdf.js and mammoth load from cdnjs; pdf.js runs its worker from a
# blob: wrapper around the cdnjs script. Fonts come from Google Fonts; favicons are data: URIs.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://cdnjs.cloudflare.com",
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
    return response

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


def _check_access(request: Request, token: Optional[str]) -> None:
    """Rate limit and access code, for every endpoint that spends money or exposes a stored deal."""
    allowed, wait = review_guard.rate_ok(request.client.host if request.client else "unknown")
    if not allowed:
        raise HTTPException(status_code=429, detail=f"Too many requests. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})
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
    out: Dict[str, Any] = {"findings": [], "web": None, "ai_error": "", "clause_error": "", "clauses": None}
    with _enforce_review_guard(request, x_review_token) as spend:
        if req.ai:
            gateway = LLMGateway()
            want_clauses = not req.lenses or "legal" in req.lenses
            deep_task = deep_review(gateway, documents, req.lenses, use_triage=req.triage, use_adjudication=req.adjudicate)
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
            if isinstance(deep_out, DeepReviewUnavailable):
                if not (req.web or (isinstance(clause_out, dict) and clause_out["findings"])):
                    raise HTTPException(status_code=503, detail=str(deep_out))
                out["ai_error"] = str(deep_out)
            elif isinstance(deep_out, BaseException):
                raise deep_out
            else:
                out.update(deep_out)
                spend.record(deep_out.get("cost_usd", 0.0))
            if isinstance(clause_out, dict):
                out["findings"] = [*out.get("findings", []), *clause_out["findings"]]
        if req.web:
            out["web"] = await web_check(documents)
    return out

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
