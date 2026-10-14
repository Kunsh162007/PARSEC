"""Download real earnings releases that contain an Adjusted EBITDA reconciliation, one per company.

Source: SEC EDGAR full-text search over 8-K exhibits. Public documents; nothing is generated.

    python -m benchmarks.ebitda.fetch --companies 40

Output (git-ignored): data/ebitda/<cik>_<accession>.txt and data/ebitda/manifest.json. Each company is assigned to
the dev or test half by a fixed rule on its CIK, so the halves never change between runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
from typing import Any, Dict, List

from benchmarks.fraud.fetch import ROOT, http_get
from benchmarks.fraud.filings import html_to_text

DATA = ROOT / "data" / "ebitda"
DEFAULT_SET = "manifest.json"
QUERY = '"reconciliation of net income to adjusted EBITDA"'
SEARCH = "https://efts.sec.gov/LATEST/search-index?q={q}&forms=8-K&dateRange=custom&startdt={start}&enddt={end}&from={offset}"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{file}"
PAGE = 100


def split_of(cik: str) -> str:
    return "test" if int(hashlib.sha256(cik.encode()).hexdigest(), 16) % 2 else "dev"


def search(start: str, end: str, wanted: int) -> List[Dict[str, Any]]:
    """One exhibit per company, from EX-99 attachments only (the press release, not the 8-K cover)."""
    seen, out, offset = set(), [], 0
    while len(out) < wanted:
        url = SEARCH.format(q=urllib.parse.quote(QUERY), start=start, end=end, offset=offset)
        hits = json.loads(http_get(url)).get("hits", {}).get("hits", [])
        if not hits:
            break
        for h in hits:
            src = h.get("_source", {})
            ciks = src.get("ciks") or []
            if not ciks or not str(src.get("file_type", "")).startswith("EX-99") or ciks[0] in seen:
                continue
            accession, filename = h["_id"].split(":", 1)
            seen.add(ciks[0])
            out.append({"company": (src.get("display_names") or [""])[0], "cik": ciks[0], "accession": accession,
                        "file": filename, "file_date": src.get("file_date", "")})
            if len(out) >= wanted:
                break
        offset += PAGE
    return out


def fetch(entries: List[Dict[str, Any]], split: str = "") -> List[Dict[str, Any]]:
    DATA.mkdir(parents=True, exist_ok=True)
    rows = []
    for e in entries:
        url = ARCHIVE.format(cik=int(e["cik"]), acc=e["accession"].replace("-", ""), file=e["file"])
        try:
            raw = http_get(url)
        except RuntimeError as error:
            print(f"skip {e['company'][:40]}: {error}", file=sys.stderr)
            continue
        text = html_to_text(raw.decode("utf-8", "replace"))
        path = DATA / f"{e['cik']}_{e['accession']}.txt"
        path.write_text(text, encoding="utf-8")
        rows.append({**e, "url": url, "split": split or split_of(e["cik"]), "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                     "chars": len(text), "sha256": hashlib.sha256(raw).hexdigest()})
        print(f"{rows[-1]['split']:4s} {e['company'][:50]}", file=sys.stderr)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--companies", type=int, default=40)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--set", default=DEFAULT_SET, help="manifest file name, so a new set never overwrites an old one")
    parser.add_argument("--exclude", default="", help="manifest whose companies must not appear in this set")
    parser.add_argument("--split", default="", help="put every release in this split (e.g. a fresh held-out 'test')")
    args = parser.parse_args()
    excluded = set()
    if args.exclude:
        excluded = {r["cik"] for r in json.loads((DATA / args.exclude).read_text(encoding="utf-8"))["releases"]}
    found = [e for e in search(args.start, args.end, args.companies + len(excluded)) if e["cik"] not in excluded][:args.companies]
    rows = fetch(found, args.split)
    (DATA / args.set).write_text(json.dumps({"query": QUERY, "dates": [args.start, args.end], "releases": rows}, indent=1), encoding="utf-8")
    print(f"{len(rows)} releases: {sum(r['split'] == 'dev' for r in rows)} dev, {sum(r['split'] == 'test' for r in rows)} test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
