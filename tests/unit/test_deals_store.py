"""A hosted copy keeps what visitors add in a database, so a restart (or a free plan going to sleep) loses nothing."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import apps.api.main as main
from packages.deals import store, workspace
from packages.review.guard import ReviewGuard

TOKEN = "access-code"
AUTH = {"X-Review-Token": TOKEN, "X-Deal-Owner": "test-visitor-0123456789"}
PDF = b"%PDF-1.4 practice"


def sqlite_store(path, limit=10_000_000):
    return store.Store(lambda: sqlite3.connect(path), placeholder="?", blob="BLOB", max_bytes=limit)


@pytest.fixture
def db(tmp_path, monkeypatch):
    saved = sqlite_store(tmp_path / "deals.db")
    monkeypatch.setattr(store, "active", lambda: saved)
    return saved


@pytest.fixture
def client(tmp_path, monkeypatch, db):
    monkeypatch.setenv("DEALS_DIR", str(tmp_path / "disk"))
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=TOKEN, per_minute=1000))
    return TestClient(main.app, headers=AUTH)


def wipe_disk(tmp_path):
    for path in sorted((tmp_path / "disk").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()


def test_uploads_and_reviews_come_back_after_the_disk_is_wiped(client, tmp_path):
    client.put("/api/v1/deals/Judge Demo/files/01 Financial/accounts.pdf", content=PDF,
               headers={"Content-Type": "application/octet-stream"})
    client.put("/api/v1/deals/Judge Demo/review", json={"dismissed": ["fin-1"]})
    wipe_disk(tmp_path)

    assert workspace.restore_saved(workspace.deals_root()) == 3   # the document, the review and who owns it

    assert client.get("/api/v1/deals/Judge Demo/files/01 Financial/accounts.pdf").content == PDF
    assert client.get("/api/v1/deals/Judge Demo/review").json()["review"] == {"dismissed": ["fin-1"]}


def test_restore_leaves_files_already_on_disk_alone(client, tmp_path):
    client.put("/api/v1/deals/Acme/files/a.txt", content=b"from the database",
               headers={"Content-Type": "application/octet-stream"})
    (tmp_path / "disk" / "Acme" / "a.txt").write_bytes(b"edited on disk")
    assert workspace.restore_saved(workspace.deals_root()) == 0
    assert (tmp_path / "disk" / "Acme" / "a.txt").read_bytes() == b"edited on disk"


def test_a_saved_file_replaces_the_earlier_copy_without_counting_twice(db):
    db.put("Acme", "a.txt", b"12345")
    db.put("Acme", "a.txt", b"123")
    assert db.total_bytes() == 3
    assert list(db.rows()) == [("Acme", "a.txt", b"123")]


def test_the_database_refuses_more_than_its_limit(tmp_path):
    small = sqlite_store(tmp_path / "small.db", limit=10)
    small.put("Acme", "a.txt", b"12345678")
    with pytest.raises(store.StoreFull):
        small.put("Acme", "b.txt", b"12345")
    small.put("Acme", "a.txt", b"1234567890")   # replacing a file only counts the difference


def test_an_upload_over_the_database_limit_is_refused_with_413(client, monkeypatch, tmp_path):
    full = sqlite_store(tmp_path / "full.db", limit=4)
    monkeypatch.setattr(store, "active", lambda: full)
    res = client.put("/api/v1/deals/Acme/files/a.pdf", content=PDF, headers={"Content-Type": "application/octet-stream"})
    assert res.status_code == 413
    assert not (tmp_path / "disk" / "Acme" / "a.pdf").exists()


def test_rows_that_would_leave_the_deals_folder_are_not_restored(tmp_path, db, monkeypatch):
    monkeypatch.setenv("DEALS_DIR", str(tmp_path / "disk"))
    for deal, path in [("..", "x.txt"), ("Acme", "../../escape.txt"), ("Acme", "run.exe"), (".hidden", "a.txt")]:
        db.put(deal, path, b"x")
    assert workspace.restore_saved(workspace.deals_root()) == 0
    assert not (tmp_path / "escape.txt").exists()


def test_without_a_database_nothing_is_mirrored(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(store, "_cached", None)
    assert store.active() is None
    assert workspace.restore_saved(tmp_path) == 0
