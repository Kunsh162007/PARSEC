"""/api/v1/deals: the deal folders on this machine, their documents, saved reviews and marked exports.

These endpoints are free (no model is called) but they read and write the disk, so they check the access code
when one is configured (REVIEW_API_TOKEN). They are not under the paid-endpoint rate limit: opening a folder fetches
every document in it. Every path is confined to the deals folder by packages/deals/workspace.py.

The page sends the visitor's demo code (X-Deal-Owner). A deal uploaded with it is listed, opened, changed and removed
only with the same code, and each code keeps its own review and marked copies, so visitors to a shared demo never see
each other's work.
"""
import json
import re
from typing import Any, Callable, Dict, List, Optional

from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from packages.deals import store, workspace
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
    except workspace.NotYours as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except store.StoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="This could not be saved right now; try again in a minute.") from exc


def _open(deal: str, token: Optional[str], key: Optional[str]) -> None:
    """Check the access code, then that this deal is shared or this visitor's own."""
    _guard(token)
    _call(lambda: workspace.require_visible(workspace.deals_root(), deal, workspace.owner_id(key)))


def list_deals(x_review_token: Optional[str] = Header(default=None), x_deal_owner: Optional[str] = Header(default=None)):
    """The shared deal folders and the ones this browser added."""
    _guard(x_review_token)
    root = workspace.deals_root()
    deals = workspace.list_deals(root, workspace.owner_id(x_deal_owner))
    return {"root": str(root), "deals": deals, "uploads": workspace.uploads_enabled()}


def list_deal_files(deal: str, x_review_token: Optional[str] = Header(default=None),
                    x_deal_owner: Optional[str] = Header(default=None)):
    """The documents in one deal folder, including its subfolders (not the marked copies)."""
    _open(deal, x_review_token, x_deal_owner)
    return {"deal": deal, "files": _call(lambda: workspace.list_documents(workspace.deals_root(), deal))}


def get_deal_file(deal: str, path: str, x_review_token: Optional[str] = Header(default=None),
                  x_deal_owner: Optional[str] = Header(default=None)):
    """One document's original bytes; the page reads PDF, Word and text itself."""
    _open(deal, x_review_token, x_deal_owner)
    target = _call(lambda: workspace.document_path(workspace.deals_root(), deal, path))
    return FileResponse(target, filename=target.name)


async def put_deal_file(deal: str, path: str, request: Request, x_review_token: Optional[str] = Header(default=None),
                        x_deal_owner: Optional[str] = Header(default=None)):
    """Add one document to this browser's deal, creating the deal on its first document. The body is the file's
    bytes; the page reads PDF, Word and text itself when the deal is opened."""
    _guard(x_review_token)
    if not workspace.uploads_enabled():
        raise HTTPException(status_code=403, detail="Adding documents is switched off on this server.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > workspace.MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="Each document may be at most 15 MB.")
    data = await request.body()
    try:
        saved = _call(lambda: workspace.save_upload(workspace.deals_root(), deal, path, data,
                                                    workspace.owner_id(x_deal_owner)))
    except workspace.TooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    return {"deal": deal, "path": saved}


def delete_deal(deal: str, x_review_token: Optional[str] = Header(default=None),
                x_deal_owner: Optional[str] = Header(default=None)):
    """Remove a deal this browser added, with its documents, review and marked copies."""
    _guard(x_review_token)
    _call(lambda: workspace.delete_deal(workspace.deals_root(), deal, workspace.owner_id(x_deal_owner)))
    return {"deal": deal, "removed": True}


def get_export_zip(deal: str, x_review_token: Optional[str] = Header(default=None),
                   x_deal_owner: Optional[str] = Header(default=None)):
    """The marked copies and summary of the last export, as one zip to download."""
    _open(deal, x_review_token, x_deal_owner)
    data = _call(lambda: workspace.export_zip(workspace.deals_root(), deal, workspace.owner_id(x_deal_owner)))
    filename = re.sub(r'[^A-Za-z0-9 ._-]', "_", deal)[:80] or "deal"
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename} - marked documents.zip"'})


def get_deal_review(deal: str, x_review_token: Optional[str] = Header(default=None),
                    x_deal_owner: Optional[str] = Header(default=None)):
    """The review saved for this deal, if there is one."""
    _open(deal, x_review_token, x_deal_owner)
    saved = _call(lambda: workspace.load_review(workspace.deals_root(), deal, workspace.owner_id(x_deal_owner)))
    return {"saved": False} if saved is None else {"saved": True, "review": saved}


async def put_deal_review(deal: str, request: Request, x_review_token: Optional[str] = Header(default=None),
                          x_deal_owner: Optional[str] = Header(default=None)):
    """Save this deal's review (findings, removed marks, decisions) into the deal folder."""
    _open(deal, x_review_token, x_deal_owner)
    raw = await request.body()
    if len(raw) > workspace.MAX_REVIEW_BYTES:
        raise HTTPException(status_code=413, detail="The review is too large to save (5 MB at most).")
    try:
        review = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=422, detail="The review must be JSON.")
    if not isinstance(review, dict):
        raise HTTPException(status_code=422, detail="The review must be a JSON object.")
    _call(lambda: workspace.save_review(workspace.deals_root(), deal, review, workspace.owner_id(x_deal_owner)))
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


def export_deal(deal: str, req: ExportRequest, x_review_token: Optional[str] = Header(default=None),
                x_deal_owner: Optional[str] = Header(default=None)):
    """Write a marked copy of every document, plus a summary, into the deal's _marked folder."""
    _open(deal, x_review_token, x_deal_owner)
    docs = [d.model_dump() for d in req.documents]
    owner = workspace.owner_id(x_deal_owner)
    written = _call(lambda: write_export(workspace.deals_root(), deal, docs, _clean_findings(req.findings), owner))
    folder = workspace.deal_dir(workspace.deals_root(), deal) / workspace.marked_dir(owner)
    return {"written": written, "folder": str(folder)}


# (method, path under /api/v1, handler, summary)
ROUTES = [
    ("GET", "/deals", list_deals, "List the deal folders"),
    ("GET", "/deals/{deal}/files", list_deal_files, "List a deal's documents"),
    ("GET", "/deals/{deal}/files/{path:path}", get_deal_file, "Fetch one document"),
    ("PUT", "/deals/{deal}/files/{path:path}", put_deal_file, "Add a document (creates the deal)"),
    ("DELETE", "/deals/{deal}", delete_deal, "Remove a deal you added"),
    ("GET", "/deals/{deal}/export.zip", get_export_zip, "Download the marked copies as a zip"),
    ("GET", "/deals/{deal}/review", get_deal_review, "Load the saved review"),
    ("PUT", "/deals/{deal}/review", put_deal_review, "Save the review"),
    ("POST", "/deals/{deal}/export", export_deal, "Write marked copies into the folder"),
]
