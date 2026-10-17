"""Deal folders on disk: one parent folder (DEALS_DIR, default ~/Deals), one subfolder per deal.

Everything stays inside that parent. A deal name is a single folder name; a document path is relative to its deal
and is resolved and checked to still be inside it, so "..", absolute paths and links out are refused. The saved
review lives in <deal>/.fineprint/review.json and marked copies in <deal>/_marked/; neither counts as a document.
"""
import io
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SUPPORTED = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".html", ".htm", ".xml", ".pdf", ".docx"}
REVIEW_DIR = ".fineprint"
MARKED_DIR = "_marked"
MAX_DOCUMENTS = 200
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
MAX_REVIEW_BYTES = 5 * 1024 * 1024
MAX_UPLOADS_PER_DEAL = 50


class UnsafePath(ValueError):
    """A deal name or document path that would leave the deals folder, or is not a document."""


class DealNotFound(LookupError):
    """No deal folder by that name."""


class TooLarge(ValueError):
    """An upload over the size or count limit."""


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


def _review_file(root: Path, name: str) -> Path:
    return deal_dir(root, name) / REVIEW_DIR / "review.json"


def _documents(folder: Path) -> List[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not _skipped(d))   # no .fineprint, _marked, hidden folders
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() in SUPPORTED and not _skipped(filename) and not path.is_symlink():
                found.append(path)
    return sorted(found, key=lambda p: p.relative_to(folder).as_posix())[:MAX_DOCUMENTS]


def list_deals(root: Path) -> List[Dict[str, Any]]:
    """Every deal folder, with how many documents it holds and when its review was last saved."""
    deals = []
    for path in sorted(p for p in root.iterdir() if p.is_dir() and not _skipped(p.name)):
        review = path / REVIEW_DIR / "review.json"
        saved = datetime.fromtimestamp(review.stat().st_mtime, timezone.utc).isoformat() if review.is_file() else None
        deals.append({"name": path.name, "documents": len(_documents(path)), "reviewed_at": saved})
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


def load_review(root: Path, name: str) -> Optional[Dict[str, Any]]:
    path = _review_file(root, name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None  # a damaged save is treated as no save; the next autosave replaces it


def write_inside(folder: Path, relative: str, content: str) -> Path:
    """Write a file under `folder` atomically (temp file, then rename), refusing anything that would leave it."""
    target = (folder / relative).resolve()
    if folder.resolve() not in target.parents:
        raise UnsafePath(f"'{relative}' is outside the deal folder.")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=target.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def save_upload(root: Path, name: str, relative: str, data: bytes) -> str:
    """Store one uploaded document in a deal, creating the deal on its first document. Returns its path."""
    folder = _deal_path(root, name)
    parts = _document_parts(relative)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise TooLarge(f"'{relative}' is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.")
    target = (folder / Path(*parts)).resolve()
    if folder not in target.parents:
        raise UnsafePath(f"'{relative}' is outside the deal folder.")
    if not target.exists() and folder.is_dir() and len(_documents(folder)) >= MAX_UPLOADS_PER_DEAL:
        raise TooLarge(f"A deal can hold at most {MAX_UPLOADS_PER_DEAL} uploaded documents.")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=target.suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target.relative_to(folder).as_posix()


def export_zip(root: Path, name: str) -> bytes:
    """The marked copies and summary of the last export, as one zip to download."""
    marked = deal_dir(root, name) / MARKED_DIR
    files = sorted(p for p in marked.rglob("*") if p.is_file() and not p.name.startswith(".")) if marked.is_dir() else []
    if not files:
        raise FileNotFoundError("Nothing has been exported for this deal yet.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(marked).as_posix())
    return buffer.getvalue()


def save_review(root: Path, name: str, review: Dict[str, Any]) -> None:
    content = json.dumps(review, ensure_ascii=False)
    if len(content.encode("utf-8")) > MAX_REVIEW_BYTES:
        raise ValueError(f"A saved review may be at most {MAX_REVIEW_BYTES // (1024 * 1024)} MB.")
    write_inside(deal_dir(root, name), f"{REVIEW_DIR}/review.json", content)
