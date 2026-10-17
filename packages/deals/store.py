"""A database copy of what visitors add to deals, so a hosted copy keeps it when its disk is wiped.

Free hosts reset the disk on every restart, including when an idle service goes to sleep. With DATABASE_URL set
(any PostgreSQL: Neon, Supabase, Render, Nebius Managed PostgreSQL), each uploaded document and saved review is also
written to one table, and packages/deals/workspace.restore_saved puts back whatever is missing at start-up. The
disk stays the working copy, so nothing else changes; without DATABASE_URL this module does nothing.
"""
import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional, Tuple

DEFAULT_MAX_BYTES = 400 * 1024 * 1024  # inside the 0.5 GB of the free PostgreSQL plans
CONNECT_TIMEOUT_SECONDS = 10


class StoreFull(ValueError):
    """Saving this would take the database past its size limit."""


class StoreUnavailable(RuntimeError):
    """The database could not be reached or refused the write."""


class Store:
    """One table, deal_files(deal, path, data, updated_at), written through any DB-API connection."""

    def __init__(self, connect: Callable[[], object], placeholder: str = "%s", blob: str = "BYTEA",
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self._connect = connect
        self._p = placeholder
        self._blob = blob
        self.max_bytes = max_bytes
        self._ready = False

    def _execute(self, sql: str, params: tuple = (), fetch: bool = False) -> list:
        try:
            with closing(self._connect()) as conn:
                cursor = conn.cursor()
                if not self._ready:
                    cursor.execute(
                        f"CREATE TABLE IF NOT EXISTS deal_files (deal TEXT NOT NULL, path TEXT NOT NULL, "
                        f"data {self._blob} NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (deal, path))")
                cursor.execute(sql.replace("?", self._p), params)
                rows = cursor.fetchall() if fetch else []
                conn.commit()
        except Exception as exc:  # every driver has its own error classes; callers only need to know it failed
            raise StoreUnavailable(f"The database could not be used: {type(exc).__name__}") from exc
        self._ready = True
        return rows

    def total_bytes(self) -> int:
        return int(self._execute("SELECT COALESCE(SUM(LENGTH(data)), 0) FROM deal_files", fetch=True)[0][0])

    def _size_of(self, deal: str, path: str) -> int:
        rows = self._execute("SELECT LENGTH(data) FROM deal_files WHERE deal = ? AND path = ?", (deal, path), fetch=True)
        return int(rows[0][0]) if rows else 0

    def put(self, deal: str, path: str, data: bytes) -> None:
        """Save one file, replacing an earlier copy at the same path."""
        if self.total_bytes() - self._size_of(deal, path) + len(data) > self.max_bytes:
            raise StoreFull(f"The saved deals are full ({self.max_bytes // (1024 * 1024)} MB).")
        self._execute(
            "INSERT INTO deal_files (deal, path, data, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (deal, path) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (deal, path, data, datetime.now(timezone.utc).isoformat()))

    def rows(self) -> Iterator[Tuple[str, str, bytes]]:
        for deal, path, data in self._execute("SELECT deal, path, data FROM deal_files ORDER BY deal, path", fetch=True):
            yield deal, path, bytes(data)


def _from_url(url: str) -> Store:
    try:
        import psycopg
    except ImportError as exc:
        raise StoreUnavailable("DATABASE_URL is set but psycopg is not installed (pip install 'psycopg[binary]').") from exc
    max_mb = os.getenv("DATABASE_MAX_MB", "").strip()
    return Store(lambda: psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_SECONDS),
                 max_bytes=int(max_mb) * 1024 * 1024 if max_mb.isdigit() else DEFAULT_MAX_BYTES)


_cached: Optional[Store] = None


def active() -> Optional[Store]:
    """The configured database, or None when DATABASE_URL is not set."""
    global _cached
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None
    if _cached is None:
        _cached = _from_url(url)
    return _cached
