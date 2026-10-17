"""On a shared demo, a deal someone uploads is theirs: only their browser lists it, opens it, adds to it or removes it."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import apps.api.main as main
from packages.deals import store, workspace
from packages.review.guard import ReviewGuard

TOKEN = "access-code"
JUDGE_A = "judge-a-0123456789abcdef"
JUDGE_B = "judge-b-0123456789abcdef"
PDF = b"%PDF-1.4 practice"


@pytest.fixture
def db(tmp_path, monkeypatch):
    saved = store.Store(lambda: sqlite3.connect(tmp_path / "deals.db"), placeholder="?", blob="BLOB")
    monkeypatch.setattr(store, "active", lambda: saved)
    return saved


@pytest.fixture
def root(tmp_path, monkeypatch, db):
    disk = tmp_path / "disk"
    (disk / "Orion Analytics").mkdir(parents=True)
    (disk / "Orion Analytics" / "accounts.txt").write_text("Revenue was $21.8m.", encoding="utf-8")
    monkeypatch.setenv("DEALS_DIR", str(disk))
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=TOKEN, per_minute=1000))
    return disk


def visitor(owner=None):
    headers = {"X-Review-Token": TOKEN}
    if owner:
        headers["X-Deal-Owner"] = owner
    return TestClient(main.app, headers=headers)


def upload(client, deal, path="a.pdf"):
    return client.put(f"/api/v1/deals/{deal}/files/{path}", content=PDF, headers={"Content-Type": "application/octet-stream"})


def names(client):
    return {d["name"]: d["removable"] for d in client.get("/api/v1/deals").json()["deals"]}


def test_each_visitor_sees_the_shared_deals_and_only_their_own_uploads(root):
    a, b = visitor(JUDGE_A), visitor(JUDGE_B)
    assert upload(a, "A's deal").status_code == 200
    assert names(a) == {"A's deal": True, "Orion Analytics": False}
    assert names(b) == {"Orion Analytics": False}
    assert names(visitor()) == {"Orion Analytics": False}


@pytest.mark.parametrize("path", ["/files", "/files/a.pdf", "/review", "/export.zip"])
def test_another_visitors_deal_cannot_be_opened(root, path):
    upload(visitor(JUDGE_A), "A's deal")
    assert visitor(JUDGE_B).get(f"/api/v1/deals/A's deal{path}").status_code == 404


def test_another_visitors_deal_cannot_be_written(root):
    upload(visitor(JUDGE_A), "A's deal")
    b = visitor(JUDGE_B)
    assert upload(b, "A's deal", "b.pdf").status_code == 403
    assert b.put("/api/v1/deals/A's deal/review", json={"dismissed": []}).status_code == 404
    assert b.post("/api/v1/deals/A's deal/export", json={"documents": [{"name": "a.pdf", "text": "x"}]}).status_code == 404


def test_shared_deals_cannot_be_added_to_or_removed(root):
    a = visitor(JUDGE_A)
    assert upload(a, "Orion Analytics", "planted.pdf").status_code == 403
    assert a.delete("/api/v1/deals/Orion Analytics").status_code == 403
    assert (root / "Orion Analytics" / "accounts.txt").is_file()
    assert not (root / "Orion Analytics" / "planted.pdf").exists()


def test_an_upload_needs_a_visitor_key(root):
    assert upload(visitor(), "Nobody's deal").status_code == 400
    assert upload(visitor("short"), "Nobody's deal").status_code == 400


def test_the_owner_removes_their_deal_from_disk_and_database(root, db):
    a = visitor(JUDGE_A)
    upload(a, "A's deal")
    a.put("/api/v1/deals/A's deal/review", json={"dismissed": ["x"]})
    assert visitor(JUDGE_B).delete("/api/v1/deals/A's deal").status_code == 404

    assert a.delete("/api/v1/deals/A's deal").status_code == 200

    assert not (root / "A's deal").exists()
    assert [row for row in db.rows() if row[0] == "A's deal"] == []
    assert workspace.restore_saved(workspace.deals_root()) == 0
    assert "A's deal" not in names(a)


def test_ownership_survives_a_wiped_disk(root, db):
    a = visitor(JUDGE_A)
    upload(a, "A's deal")
    for path in sorted((root / "A's deal").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (root / "A's deal").rmdir()

    workspace.restore_saved(workspace.deals_root())

    assert names(a) == {"A's deal": True, "Orion Analytics": False}
    assert names(visitor(JUDGE_B)) == {"Orion Analytics": False}


def test_the_visitor_key_is_not_stored_as_given(root):
    upload(visitor(JUDGE_A), "A's deal")
    stored = (root / "A's deal" / ".fineprint" / "owner").read_text(encoding="utf-8")
    assert JUDGE_A not in stored and len(stored) == 64
