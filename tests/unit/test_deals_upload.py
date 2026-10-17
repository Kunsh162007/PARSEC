"""Visitors on a hosted copy bring their own documents: upload into a new deal, and take the marked copies home."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

import apps.api.main as main
from packages.deals import workspace
from packages.review.guard import ReviewGuard

TOKEN = "access-code"
AUTH = {"X-Review-Token": TOKEN, "X-Deal-Owner": "test-visitor-0123456789"}
PDF = b"%PDF-1.4 practice"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEALS_DIR", str(tmp_path))
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=TOKEN, per_minute=1000))
    return TestClient(main.app, headers=AUTH)


def put(client, deal, path, data=PDF):
    return client.put(f"/api/v1/deals/{deal}/files/{path}", content=data,
                      headers={**AUTH, "Content-Type": "application/octet-stream"})


def test_uploading_files_creates_the_deal_and_keeps_subfolders(client, tmp_path):
    assert put(client, "Judge Demo", "01 Financial/accounts.pdf").status_code == 200
    assert put(client, "Judge Demo", "contract.txt", b"Liability is unlimited.").status_code == 200
    files = [f["path"] for f in client.get("/api/v1/deals/Judge Demo/files").json()["files"]]
    assert files == ["01 Financial/accounts.pdf", "contract.txt"]
    assert (tmp_path / "Judge Demo" / "01 Financial" / "accounts.pdf").read_bytes() == PDF


@pytest.mark.parametrize("deal,path", [
    ("..", "a.pdf"), ("Demo", "../escape.pdf"), ("Demo", "/abs.pdf"), ("Demo", "C:/x.pdf"),
    ("Demo", ".fineprint/review.json"), ("Demo", "_marked/x.pdf"), ("Demo", "script.exe"), (".hidden", "a.pdf"),
])
def test_uploads_cannot_leave_the_deal_or_bring_other_file_types(client, deal, path):
    assert put(client, deal, path).status_code in (400, 404, 422)


def test_an_oversized_upload_is_refused(client, monkeypatch):
    monkeypatch.setattr(workspace, "MAX_DOCUMENT_BYTES", 10)
    assert put(client, "Demo", "big.pdf", b"x" * 11).status_code == 413


def test_a_deal_takes_a_limited_number_of_uploads(client, monkeypatch):
    monkeypatch.setattr(workspace, "MAX_UPLOADS_PER_DEAL", 2)
    assert put(client, "Demo", "a.pdf").status_code == 200
    assert put(client, "Demo", "b.pdf").status_code == 200
    assert put(client, "Demo", "a.pdf").status_code == 200        # replacing a file is fine
    assert put(client, "Demo", "c.pdf").status_code == 413


def test_uploads_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("DEAL_UPLOADS", "off")
    assert put(client, "Demo", "a.pdf").status_code == 403
    assert client.get("/api/v1/deals").json()["uploads"] is False


def test_uploads_need_the_access_code_when_one_is_set(client):
    res = client.put("/api/v1/deals/Demo/files/a.pdf", content=PDF, headers={"X-Review-Token": "wrong"})
    assert res.status_code == 401


def test_the_marked_copies_download_as_one_zip(client):
    put(client, "Demo", "contract.txt", b"Liability is unlimited.")
    docs = [{"name": "contract.txt", "text": "Liability is unlimited."}]
    findings = [{"id": "f1", "severity": "high", "title": "Uncapped liability", "why": "w", "ask": "a",
                 "spans": [{"doc": 0, "start": 0, "end": 9}]}]
    assert client.post("/api/v1/deals/Demo/export", json={"documents": docs, "findings": findings}).status_code == 200
    res = client.get("/api/v1/deals/Demo/export.zip")
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert "summary.html" in names and "contract.txt.marked.html" in names


def test_no_zip_before_an_export(client):
    put(client, "Demo", "a.pdf")
    assert client.get("/api/v1/deals/Demo/export.zip").status_code == 404


def test_the_practice_deal_downloads_as_a_folder_ready_to_add(client):
    res = client.get("/api/v1/practice-deal.zip")
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert all(n.startswith("Orion Analytics/") for n in names)
    assert sum(n.endswith(".pdf") for n in names) == 14
    assert not any("/_" in n for n in names)
