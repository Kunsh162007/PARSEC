"""Deal folders on disk: one parent folder (DEALS_DIR, default ~/Deals), one subfolder per deal.

Everything stays inside that parent. A deal name is a single folder name; a document path is relative to its deal
and is resolved and checked to still be inside it, so "..", absolute paths and links out are refused. The saved
review lives in <deal>/.fineprint/review.json and marked copies in <deal>/_marked/; neither counts as a document.

On a shared demo each visitor has a demo code. A deal added through the page belongs to its code:
<deal>/.fineprint/owner holds a digest of the code, and only that code lists, opens, adds to or removes the deal.
Folders without that file (the practice deal, folders on this computer) are shared: everyone sees them and nobody can
change them from the page. Each code also keeps its own review (.fineprint/reviews/<digest>.json) and its own marked
copies (_marked/<digest prefix>/) of every deal it opens, shared ones included.
"""
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from packages.deals import store

logger = logging.getLogger("parsec.deals")

SUPPORTED = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".html", ".htm", ".xml", ".pdf", ".docx"}
REVIEW_DIR = ".fineprint"
MARKED_DIR = "_marked"
MAX_DOCUMENTS = 200
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
MAX_REVIEW_BYTES = 5 * 1024 * 1024
MAX_UPLOADS_PER_DEAL = 50
REVIEW_PATH = f"{REVIEW_DIR}/review.json"
OWNER_PATH = f"{REVIEW_DIR}/owner"
VISITOR_KEY = re.compile(r"[A-Za-z0-9_-]{12,128}")
OWN_REVIEW = re.compile(r"\.fineprint/reviews/[0-9a-f]{64}\.json")


class UnsafePath(ValueError):
    """A deal name or document path that would leave the deals folder, or is not a document."""


class DealNotFound(LookupError):
    """No deal folder by that name."""


class TooLarge(ValueError):
    """An upload over the size or count limit."""


class NotYours(PermissionError):
    """A shared deal, or one another visitor added: it cannot be changed from the page."""


def deals_root() -> Path:
    """The parent folder of all deals, created on first use."""
    root = Path(os.getenv("DEALS_DIR") or Path.home() / "Deals").expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _skipped(part: str) -> bool:
    return part.startswith((".", "_"))


def uploads_enabled() -> bool:
    """Whether visitors may add documents through the page (DEAL_UPLOADS=off turns it off)."""
    return os.getenv("DEAL_UPLOADS", "on").strip().lower() not in {"off", "0", "false", "no"}


def _deal_path(root: Path, name: str) -> Path:
    if not name or name != name.strip() or len(name) > 120 or any(c in name for c in "/\\:") or _skipped(name):
        raise UnsafePath(f"'{name}' is not a deal folder name.")
    path = (root / name).resolve()
    if path.parent != root:
        raise UnsafePath(f"'{name}' is outside the deals folder.")
    return path


def _document_parts(relative: str) -> tuple:
    parts = Path(relative.replace("\\", "/")).parts
    if not parts or Path(relative).is_absolute() or ":" in relative or any(p == ".." or _skipped(p) for p in parts):
        raise UnsafePath(f"'{relative}' is not a document in this deal.")
    if Path(parts[-1]).suffix.lower() not in SUPPORTED:
        raise UnsafePath(f"'{relative}' is not a supported document type.")
    return parts


def deal_dir(root: Path, name: str) -> Path:
    """The folder of one deal. The name must be exactly one visible folder directly inside the root."""
    path = _deal_path(root, name)
    if not path.is_dir():
        raise DealNotFound(f"There is no deal folder called '{name}'.")
    return path


def owner_id(key: Optional[str]) -> Optional[str]:
    """The stored form of a demo code (the SHA-256 digest of it in capitals), or None when it is missing or malformed."""
    key = (key or "").strip()
    if not VISITOR_KEY.fullmatch(key):
        return None
    return hashlib.sha256(key.upper().encode("ascii")).hexdigest()


def review_path(owner: Optional[str]) -> str:
    """Where a review is kept, relative to its deal: one file per demo code, or the folder's own without one."""
    return f"{REVIEW_DIR}/reviews/{owner}.json" if owner else REVIEW_PATH


def marked_dir(owner: Optional[str]) -> str:
    return f"{MARKED_DIR}/{owner[:16]}" if owner else MARKED_DIR


def _owner_of(folder: Path) -> Optional[str]:
    marker = folder / OWNER_PATH
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else None


def require_visible(root: Path, name: str, owner: Optional[str]) -> None:
    """Refuse, as if it did not exist, a deal another visitor added."""
    found = _owner_of(deal_dir(root, name))
    if found is not None and found != owner:
        raise DealNotFound(f"There is no deal folder called '{name}'.")


def _documents(folder: Path) -> List[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not _skipped(d))   # no .fineprint, _marked, hidden folders
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() in SUPPORTED and not _skipped(filename) and not path.is_symlink():
                found.append(path)
    return sorted(found, key=lambda p: p.relative_to(folder).as_posix())[:MAX_DOCUMENTS]


def list_deals(root: Path, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """The shared deals and this visitor's own, with how many documents each holds and when it was last reviewed."""
    deals = []
    for path in sorted(p for p in root.iterdir() if p.is_dir() and not _skipped(p.name)):
        found = _owner_of(path)
        if found is not None and found != owner:
            continue
        review = path / review_path(owner)
        saved = datetime.fromtimestamp(review.stat().st_mtime, timezone.utc).isoformat() if review.is_file() else None
        deals.append({"name": path.name, "documents": len(_documents(path)), "reviewed_at": saved,
                      "removable": found is not None})
    return deals


def list_documents(root: Path, name: str) -> List[Dict[str, Any]]:
    folder = deal_dir(root, name)
    return [
        {"path": p.relative_to(folder).as_posix(), "size": p.stat().st_size,
         "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
        for p in _documents(folder)
    ]


def document_path(root: Path, name: str, relative: str) -> Path:
    """A document inside the deal, by its path relative to the deal folder."""
    folder = deal_dir(root, name)
    parts = _document_parts(relative)
    path = (folder / Path(*parts)).resolve()
    if folder not in path.parents:
        raise UnsafePath(f"'{relative}' is outside the deal folder.")
    if not path.is_file():
        raise FileNotFoundError(f"There is no document '{relative}' in this deal.")
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise UnsafePath(f"'{relative}' is larger than 15 MB.")
    return path


def load_review(root: Path, name: str, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = deal_dir(root, name) / review_path(owner)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None  # a damaged save is treated as no save; the next autosave replaces it


def _write_atomic(target: Path, data: bytes) -> None:
    """Write through a temp file and a rename, so a reader never sees half a file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=target.suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_inside(folder: Path, relative: str, content: str) -> Path:
    """Write a text file under `folder` atomically, refusing anything that would leave it."""
    target = (folder / relative).resolve()
    if folder.resolve() not in target.parents:
        raise UnsafePath(f"'{relative}' is outside the deal folder.")
    _write_atomic(target, content.encode("utf-8"))
    return target


def _mirror(name: str, relative: str, data: bytes) -> None:
    """Copy a file into the database first, when there is one, so it is never on disk only."""
    saved = store.active()
    if saved is None:
        return
    try:
        saved.put(name, relative, data)
    except store.StoreFull as exc:
        raise TooLarge(str(exc)) from exc


def save_upload(root: Path, name: str, relative: str, data: bytes, owner: Optional[str]) -> str:
    """Store one uploaded document in this visitor's deal, creating the deal on its first document. Returns its path."""
    folder = _deal_path(root, name)
    parts = _document_parts(relative)
    if owner is None:
        raise UnsafePath("Adding documents needs this browser's visitor key. Reload the page and try again.")
    if folder.is_dir() and _owner_of(folder) != owner:
        raise NotYours(f"'{name}' is already a deal here. Choose another name for yours.")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise TooLarge(f"'{relative}' is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.")
    target = (folder / Path(*parts)).resolve()
    if folder not in target.parents:
        raise UnsafePath(f"'{relative}' is outside the deal folder.")
    if not target.exists() and folder.is_dir() and len(_documents(folder)) >= MAX_UPLOADS_PER_DEAL:
        raise TooLarge(f"A deal can hold at most {MAX_UPLOADS_PER_DEAL} uploaded documents.")
    if not folder.is_dir():
        _mirror(name, OWNER_PATH, owner.encode("ascii"))
        _write_atomic(folder / OWNER_PATH, owner.encode("ascii"))
    saved = target.relative_to(folder).as_posix()
    _mirror(name, saved, data)
    _write_atomic(target, data)
    return saved


def delete_deal(root: Path, name: str, owner: Optional[str]) -> None:
    """Remove a deal this visitor added, from the database and the disk. Shared deals are never removed."""
    folder = deal_dir(root, name)
    found = _owner_of(folder)
    if found is None:
        raise NotYours(f"'{name}' is shared, so it cannot be removed from the page.")
    if found != owner:
        raise DealNotFound(f"There is no deal folder called '{name}'.")
    saved = store.active()
    if saved is not None:
        saved.delete(name)
    shutil.rmtree(folder)


def export_zip(root: Path, name: str, owner: Optional[str] = None) -> bytes:
    """The marked copies and summary of this demo code's last export, as one zip to download."""
    marked = deal_dir(root, name) / marked_dir(owner)
    files = sorted(p for p in marked.iterdir() if p.is_file() and not p.name.startswith(".")) if marked.is_dir() else []
    if not files:
        raise FileNotFoundError("Nothing has been exported for this deal yet.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(marked).as_posix())
    return buffer.getvalue()


def save_review(root: Path, name: str, review: Dict[str, Any], owner: Optional[str] = None) -> None:
    content = json.dumps(review, ensure_ascii=False)
    if len(content.encode("utf-8")) > MAX_REVIEW_BYTES:
        raise ValueError(f"A saved review may be at most {MAX_REVIEW_BYTES // (1024 * 1024)} MB.")
    folder = deal_dir(root, name)
    _mirror(name, review_path(owner), content.encode("utf-8"))
    write_inside(folder, review_path(owner), content)


def _restore_target(root: Path, name: str, relative: str) -> Path:
    folder = _deal_path(root, name)
    if relative in (REVIEW_PATH, OWNER_PATH) or OWN_REVIEW.fullmatch(relative):
        return folder / relative
    target = (folder / Path(*_document_parts(relative))).resolve()
    if folder not in target.parents:
        raise UnsafePath(f"'{relative}' is outside the deal folder.")
    return target


def restore_saved(root: Path) -> int:
    """Put back every file the database holds that is missing on disk; returns how many were written.

    Rows are checked like any upload, so a row that names a path outside the deals folder is skipped."""
    saved = store.active()
    if saved is None:
        return 0
    restored = 0
    for name, relative, data in saved.rows():
        try:
            target = _restore_target(root, name, relative)
        except UnsafePath:
            logger.warning("Skipped a saved file that is not a document in a deal: %r / %r", name, relative)
            continue
        if not target.exists():
            _write_atomic(target, data)
            restored += 1
    return restored
