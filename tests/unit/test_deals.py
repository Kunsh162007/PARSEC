"""Deal folders: one parent folder on disk, one subfolder per deal. Reviews and marked copies stay inside the deal."""
import json

import pytest
from fastapi.testclient import TestClient

import apps.api.main as main
from packages.deals import workspace
from packages.deals.export import marked_html, write_export
from packages.deals.workspace import UnsafePath
from packages.review.guard import ReviewGuard

TOKEN = "access-code"
AUTH = {"X-Review-Token": TOKEN}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("DEALS_DIR", str(tmp_path))
    acme = tmp_path / "Acme"
    (acme / "01 Financial").mkdir(parents=True)
    (acme / "cim.txt").write_text("Revenue for FY2024 was $10.0 million.", encoding="utf-8")
    (acme / "01 Financial" / "accounts.txt").write_text("Revenue for FY2024 was $9.1 million.", encoding="utf-8")
    (acme / "photo.png").write_bytes(b"\x89PNG")
    (acme / "_marked").mkdir()
    (acme / "_marked" / "old.marked.html").write_text("x", encoding="utf-8")
    (tmp_path / "Beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("not a deal", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(root, monkeypatch):
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=TOKEN, per_minute=1000))
    return TestClient(main.app, headers=AUTH)


# ---- the workspace

def test_each_visible_subfolder_is_a_deal(root):
    deals = {d["name"]: d for d in workspace.list_deals(workspace.deals_root())}
    assert set(deals) == {"Acme", "Beta"}
    assert deals["Acme"]["documents"] == 2
    assert deals["Acme"]["reviewed_at"] is None


def test_documents_include_subfolders_but_not_marked_copies_or_unsupported_files(root):
    files = [f["path"] for f in workspace.list_documents(workspace.deals_root(), "Acme")]
    assert files == ["01 Financial/accounts.txt", "cim.txt"]


@pytest.mark.parametrize("name", ["..", ".", "../etc", "Acme/../..", "", ".hidden", "Acme\\..", "C:\\Windows"])
def test_deal_names_cannot_leave_the_deals_folder(root, name):
    with pytest.raises((UnsafePath, workspace.DealNotFound)):
        workspace.deal_dir(workspace.deals_root(), name)


@pytest.mark.parametrize("path", ["../Beta/x.txt", "../../secret.txt", "/etc/passwd", "photo.png",
                                  "_marked/old.marked.html", "missing.txt", "C:/Windows/win.ini"])
def test_document_paths_cannot_leave_the_deal_or_reach_unsupported_files(root, path):
    with pytest.raises((UnsafePath, FileNotFoundError)):
        workspace.document_path(workspace.deals_root(), "Acme", path)


def test_a_review_is_saved_inside_the_deal_and_read_back(root):
    base = workspace.deals_root()
    assert workspace.load_review(base, "Acme") is None
    workspace.save_review(base, "Acme", {"dismissed": ["leg-coc"], "notes": "checked"})
    assert workspace.load_review(base, "Acme")["dismissed"] == ["leg-coc"]
    assert (root / "Acme" / ".fineprint" / "review.json").is_file()
    assert next(d for d in workspace.list_deals(base) if d["name"] == "Acme")["reviewed_at"]


def test_review_file_is_valid_json(root):
    workspace.save_review(workspace.deals_root(), "Beta", {"a": 1})
    assert json.loads((root / "Beta" / ".fineprint" / "review.json").read_text(encoding="utf-8")) == {"a": 1}


# ---- export

FINDINGS = [
    {"id": "int-conflict-1", "severity": "medium", "title": "Different figures given for revenue",
     "why": "One is wrong.", "ask": "Ask which is right.", "detail": "9.1m versus 10.0m",
     "spans": [{"doc": 0, "start": 0, "end": 7}, {"doc": 1, "start": 0, "end": 7}]},
    {"id": "x-<script>", "severity": "high", "title": "<script>alert(1)</script>", "why": "w", "ask": "a",
     "spans": [{"doc": 0, "start": 8, "end": 11}]},
]
DOCS = [{"name": "cim.txt", "text": "Revenue for FY2024 was $10.0 million."},
        {"name": "01 Financial/accounts.txt", "text": "Revenue for FY2024 was $9.1 million."}]


def test_marked_copy_highlights_each_problem_and_explains_it():
    html = marked_html(DOCS, 0, FINDINGS)
    assert "<mark" in html and "Revenue" in html
    assert "Different figures given for revenue" in html and "Ask which is right." in html
    assert "01 Financial__accounts.txt.marked.html" in html   # the link to the other document's copy


def test_marked_copy_has_the_colour_legend():
    html = marked_html(DOCS, 0, FINDINGS)
    for label in ("Critical", "High", "Medium", "Low", "Also in another document"):
        assert label in html
    assert "#dc2626" in html and "#ea580c" in html   # red for critical, orange for high


def test_marked_copy_escapes_everything_from_the_documents_and_findings():
    html = marked_html(DOCS, 0, FINDINGS)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_odd_values_from_the_page_do_not_break_the_export():
    odd = [{"id": 7, "severity": 3, "title": None, "why": "w", "ask": "a", "spans": [{"doc": 0, "start": 0, "end": 4}]}]
    html = marked_html(DOCS, 0, odd)
    assert "<mark" in html


def test_export_writes_marked_copies_and_a_summary_inside_the_deal(root):
    written = write_export(workspace.deals_root(), "Acme", DOCS, FINDINGS)
    assert set(written) == {"_marked/cim.txt.marked.html", "_marked/01 Financial__accounts.txt.marked.html",
                            "_marked/summary.html"}
    for rel in written:
        assert (root / "Acme" / rel).is_file()


# ---- the API

def test_api_lists_deals_and_their_files(client):
    deals = client.get("/api/v1/deals").json()
    assert [d["name"] for d in deals["deals"]] == ["Acme", "Beta"]
    files = client.get("/api/v1/deals/Acme/files").json()["files"]
    assert [f["path"] for f in files] == ["01 Financial/accounts.txt", "cim.txt"]


def test_api_serves_a_document_from_a_subfolder(client):
    res = client.get("/api/v1/deals/Acme/files/01 Financial/accounts.txt")
    assert res.status_code == 200 and "9.1 million" in res.text


@pytest.mark.parametrize("path", ["/api/v1/deals/Acme/files/..%2F..%2Fnotes.txt", "/api/v1/deals/Acme/files/photo.png",
                                  "/api/v1/deals/..%2F/files/x.txt", "/api/v1/deals/Nope/files"])
def test_api_refuses_paths_outside_the_deal(client, path):
    assert client.get(path).status_code in (400, 404)


def test_api_saves_and_loads_a_review(client):
    assert client.get("/api/v1/deals/Acme/review").json() == {"saved": False}
    assert client.put("/api/v1/deals/Acme/review", json={"dismissed": ["a"]}).status_code == 200
    body = client.get("/api/v1/deals/Acme/review").json()
    assert body["saved"] is True and body["review"]["dismissed"] == ["a"]


def test_api_rejects_an_oversized_review(client):
    res = client.put("/api/v1/deals/Acme/review", json={"blob": "x" * (workspace.MAX_REVIEW_BYTES + 1)})
    assert res.status_code == 413


def test_api_export_writes_into_the_deal(client, root):
    res = client.post("/api/v1/deals/Acme/export", json={"documents": DOCS, "findings": FINDINGS})
    assert res.status_code == 200
    assert "_marked/summary.html" in res.json()["written"]
    assert (root / "Acme" / "_marked" / "summary.html").is_file()


def test_api_needs_the_access_code_when_one_is_set(client):
    # Folder access is free, but it reads and writes your disk: a configured access code applies to it.
    assert client.get("/api/v1/deals", headers={"X-Review-Token": "wrong"}).status_code == 401


def test_folder_access_is_not_slowed_by_the_paid_rate_limit(root, monkeypatch):
    monkeypatch.setattr(main, "review_guard", ReviewGuard(token=TOKEN, per_minute=1))
    client = TestClient(main.app, headers=AUTH)
    assert all(client.get("/api/v1/deals/Acme/files").status_code == 200 for _ in range(5))


def test_parent_index_lists_the_deal_endpoints(client):
    paths = {e["path"] for e in client.get("/api/v1").json()["endpoints"]}
    assert {"/api/v1/deals", "/api/v1/deals/{deal}/export"} <= paths
