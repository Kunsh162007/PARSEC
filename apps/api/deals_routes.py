"""/api/v1/deals: the deal folders on this machine, their documents, saved reviews and marked exports.

These endpoints are free (no model is called) but they read and write the disk, so they check the access code
when one is configured (REVIEW_API_TOKEN). They are not under the paid-endpoint rate limit: opening a folder fetches
every document in it. Every path is confined to the deals folder by packages/deals/workspace.py.
"""
import json
from typing import Any, Callable, Dict, List, Optional

from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from packages.deals import workspace
from packages.deals.export import write_export

# Set by apps/api/main.py: whether a given access code is accepted (always True when none is configured).
token_check: Callable[[Optional[str]], bool] = lambda _token: True

MAX_EXPORT_CHARS = 2_000_000
MAX_EXPORT_FINDINGS = 2_000


def _guard(token: Optional[str]) -> None:
    if not token_check(token):
        raise HTTPException(status_code=401, detail="An access code is required, or the one given is wrong.")


def _call(fn: Callable[[], Any]) -> Any:
    """Run a workspace call, turning its refusals into HTTP answers."""
    try:
        return fn()
    except workspace.UnsafePath as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (workspace.DealNotFound, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def list_deals(x_review_token: Optional[str] = Header(default=None)):
    """Every deal folder: put one folder per deal inside the deals folder, with its documents in it."""
    _guard(x_review_token)
    root = workspace.deals_root()
    return {"root": str(root), "deals": workspace.list_deals(root)}


def list_deal_files(deal: str, x_review_token: Optional[str] = Header(default=None)):
    """The documents in one deal folder, including its subfolders (not the marked copies)."""
    _guard(x_review_token)
    return {"deal": deal, "files": _call(lambda: workspace.list_documents(workspace.deals_root(), deal))}


def get_deal_file(deal: str, path: str, x_review_token: Optional[str] = Header(default=None)):
    """One document's original bytes; the page reads PDF, Word and text itself."""
    _guard(x_review_token)
    target = _call(lambda: workspace.document_path(workspace.deals_root(), deal, path))
    return FileResponse(target, filename=target.name)


def get_deal_review(deal: str, x_review_token: Optional[str] = Header(default=None)):
    """The review saved for this deal, if there is one."""
    _guard(x_review_token)
    saved = _call(lambda: workspace.load_review(workspace.deals_root(), deal))
    return {"saved": False} if saved is None else {"saved": True, "review": saved}


async def put_deal_review(deal: str, request: Request, x_review_token: Optional[str] = Header(default=None)):
    """Save this deal's review (findings, removed marks, decisions) into the deal folder."""
    _guard(x_review_token)
    raw = await request.body()
    if len(raw) > workspace.MAX_REVIEW_BYTES:
        raise HTTPException(status_code=413, detail="The review is too large to save (5 MB at most).")
    try:
        review = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=422, detail="The review must be JSON.")
    if not isinstance(review, dict):
        raise HTTPException(status_code=422, detail="The review must be a JSON object.")
    _call(lambda: workspace.save_review(workspace.deals_root(), deal, review))
    return {"saved": True}


class ExportDocument(BaseModel):
    name: str = Field(..., min_length=1, max_length=400)
    text: str = Field(..., max_length=MAX_EXPORT_CHARS)


class ExportRequest(BaseModel):
    documents: List[ExportDocument] = Field(..., min_length=1, max_length=workspace.MAX_DOCUMENTS)
    findings: List[Dict[str, Any]] = Field(default_factory=list, max_length=MAX_EXPORT_FINDINGS)


def _clean_findings(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Findings with an id, keeping only well-formed spans."""
    clean = []
    for f in raw:
        if not f.get("id") or not isinstance(f.get("spans"), list):
            continue
        spans = [s for s in f["spans"] if isinstance(s, dict) and all(isinstance(s.get(k), int) for k in ("doc", "start", "end"))]
        clean.append({**f, "spans": spans})
    return clean


def export_deal(deal: str, req: ExportRequest, x_review_token: Optional[str] = Header(default=None)):
    """Write a marked copy of every document, plus a summary, into the deal's _marked folder."""
    _guard(x_review_token)
    docs = [d.model_dump() for d in req.documents]
    written = _call(lambda: write_export(workspace.deals_root(), deal, docs, _clean_findings(req.findings)))
    return {"written": written, "folder": str(workspace.deal_dir(workspace.deals_root(), deal) / workspace.MARKED_DIR)}


# (method, path under /api/v1, handler, summary)
ROUTES = [
    ("GET", "/deals", list_deals, "List the deal folders"),
    ("GET", "/deals/{deal}/files", list_deal_files, "List a deal's documents"),
    ("GET", "/deals/{deal}/files/{path:path}", get_deal_file, "Fetch one document"),
    ("GET", "/deals/{deal}/review", get_deal_review, "Load the saved review"),
    ("PUT", "/deals/{deal}/review", put_deal_review, "Save the review"),
    ("POST", "/deals/{deal}/export", export_deal, "Write marked copies into the folder"),
]
