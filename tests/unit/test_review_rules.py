"""The server runs the same rules engine as the browser (apps/review/engine.js) through Node."""
import asyncio

import pytest

from packages.review import rules
from packages.review.deep import LENS_IDS
from packages.review.rules import RulesUnavailable, categories, normalize_text, run_rules

CONTRACT = (
    "\U0001F600 The Supplier may terminate on a change of control of the Customer. "
    "Liability is unlimited. Insurance valid until 14 March 2023."
)


def analyse(text: str, lenses, as_of: str = "2026-01-01"):
    return asyncio.run(run_rules([{"name": "a.txt", "text": text}], lenses, as_of))


def test_engine_categories_are_the_server_lens_ids():
    listed = categories()
    assert {c["id"] for c in listed} == LENS_IDS
    assert all(c["name"] and c["description"] for c in listed)


def test_only_the_requested_category_and_integrity_are_returned():
    out = analyse(CONTRACT, ["legal"])
    lenses = {f["lens"] for f in out["findings"]}
    assert "legal" in lenses
    assert lenses <= {"legal", "integrity"}
    assert all(f["source"] == "rules" for f in out["findings"])


def test_offsets_are_code_points_into_the_text():
    out = analyse(CONTRACT, ["legal"])
    coc = next(f for f in out["findings"] if f["id"] == "leg-coc")
    span = coc["spans"][0]
    assert CONTRACT[span["start"]:span["end"]] == span["text"]
    assert "change of control" in span["text"]


def test_as_of_date_drives_the_expiry_check():
    before = {f["id"] for f in analyse(CONTRACT, [], as_of="2023-01-01")["findings"]}
    after = {f["id"] for f in analyse(CONTRACT, [], as_of="2026-01-01")["findings"]}
    assert "int-expired" not in before
    assert "int-expired" in after


def test_missing_node_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(rules, "_node_binary", lambda: None)
    with pytest.raises(RulesUnavailable, match="Node.js"):
        analyse(CONTRACT, ["legal"])


def test_line_endings_are_normalised_like_the_browser_engine():
    assert normalize_text("a\r\nb\rc\n") == "a\nb\nc\n"
