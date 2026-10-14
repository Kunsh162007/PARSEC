"""Fetch the company's own annual reports for the periods an SEC order says were affected.

For each case already downloaded by benchmarks.fraud.fetch this resolves the company on EDGAR, reads which fiscal
years the order concerns, and downloads the 10-K (and 10-K/A) for those years. These are the documents a reviewer
would have had before the problem became public.

    python -m benchmarks.fraud.filings --cases 20

Output (git-ignored): data/fraud/filings/<AAER>/<form>_<period>.txt and data/fraud/filings_manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.parse
from collections import Counter
from datetime import datetime, timezone

from benchmarks.fraud.fetch import DATA, ROOT, http_get

SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?q={q}&forms=10-K"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
YEAR_CONTEXT = re.compile(
    r"(?:fiscal\s+(?:years?\s+)?(?:ended\s+)?|year[s]?\s+ended\s+|FY\s?|for\s+(?:the\s+)?(?:year|period)s?\s+)"
    r"(?:[A-Z][a-z]+\.?\s+\d{1,2},\s+)?((?:19|20)\d{2})",
    re.IGNORECASE,
)
ANY_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
MAX_FILINGS_PER_CASE = 4
MIN_YEAR, MAX_YEAR = 1995, 2026


def core_name(respondent: str) -> str:
    first = re.split(r"\s+and\s+|;|\bet al\b", respondent)[0]
    first = re.sub(r"\(f/k/a[^)]*\)", "", first)
    return re.sub(r"[,.\s]+$", "", first).strip()


def resolve_cik(name: str) -> tuple[str, str] | None:
    """CIK of the company whose name best matches, taken from EDGAR full-text hits. None if nothing matches."""
    query = urllib.parse.quote('"' + core_name(name) + '"')
    try:
        data = json.loads(http_get(SEARCH_URL.format(q=query)))
    except (RuntimeError, ValueError):
        return None
    wanted = re.sub(r"[^a-z0-9 ]", "", core_name(name).lower())
    votes: Counter[tuple[str, str]] = Counter()
    for hit in data.get("hits", {}).get("hits", [])[:20]:
        source = hit.get("_source", {})
        for display, cik in zip(source.get("display_names", []), source.get("ciks", [])):
            label = re.sub(r"[^a-z0-9 ]", "", display.lower())
            if wanted.split(" ")[0] in label:
                votes[(cik, display)] += 1
    return votes.most_common(1)[0][0] if votes else None


def affected_years(order_text: str) -> list[int]:
    """Fiscal years the order is about: explicit 'fiscal year' contexts first, else the most frequent plain years."""
    explicit = [int(y) for y in YEAR_CONTEXT.findall(order_text)]
    years = [y for y in explicit if MIN_YEAR <= y <= MAX_YEAR]
    if not years:
        plain = [int(y) for y in ANY_YEAR.findall(order_text)]
        years = [y for y, _ in Counter(y for y in plain if MIN_YEAR <= y <= MAX_YEAR).most_common(3)]
    return sorted(set(years))


def list_annual_filings(cik: str) -> list[dict]:
    data = json.loads(http_get(SUBMISSIONS_URL.format(cik=cik.zfill(10))))
    recent = data["filings"]["recent"]
    rows = [dict(zip(recent.keys(), values)) for values in zip(*recent.values())]
    return [r for r in rows if r["form"] in {"10-K", "10-K/A", "10-K405", "10-KSB"}]


def _cell_text(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def _table_row(match: "re.Match[str]") -> str:
    """One table row on one line. '$' and a closing ')' or '%' sit in their own cells in filings; rejoin them."""
    cells = [_cell_text(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", match.group(1))]
    merged: list[str] = []
    pending_prefix = ""
    for c in cells:
        if not c:
            continue
        if c in ("$", "€", "£"):
            pending_prefix = c
            continue
        if merged and (c.startswith(")") or c == "%"):
            merged[-1] += c
            continue
        merged.append(pending_prefix + c)
        pending_prefix = ""
    return "\n" + " | ".join(merged) + "\n" if merged else "\n"


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    # Inline XBRL puts a hidden block of machine tags at the top of the page; it is not part of the document.
    raw = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", raw)
    raw = re.sub(r"(?is)<div[^>]*display:\s*none[^>]*>.*?</div>", " ", raw)
    raw = re.sub(r"(?is)<tr[^>]*>(.*?)</tr>", _table_row, raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", raw)
    raw = re.sub(r"(?i)</t[dh]>", " | ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def fetch_case(case: dict, order_text: str) -> list[dict]:
    resolved = resolve_cik(case["respondent"])
    if not resolved:
        print(f"  {case['aaer']}: company not resolved on EDGAR", file=sys.stderr)
        return []
    cik, display = resolved
    years = affected_years(order_text)
    filings = [f for f in list_annual_filings(cik) if f.get("reportDate") and int(f["reportDate"][:4]) in years]
    filings.sort(key=lambda f: f["reportDate"])
    out = []
    target = DATA / "filings" / case["aaer"]
    target.mkdir(parents=True, exist_ok=True)
    for filing in filings[:MAX_FILINGS_PER_CASE]:
        url = ARCHIVE_URL.format(cik=int(cik), acc=filing["accessionNumber"].replace("-", ""), doc=filing["primaryDocument"])
        try:
            raw = http_get(url)
        except RuntimeError as error:
            print(f"  skip {url}: {error}", file=sys.stderr)
            continue
        text = html_to_text(raw.decode("utf-8", "replace"))
        path = target / f"{filing['form'].replace('/', '-')}_{filing['reportDate']}.txt"
        path.write_text(text, encoding="utf-8")
        out.append({
            "aaer": case["aaer"], "respondent": case["respondent"], "edgar_name": display, "cik": cik,
            "form": filing["form"], "report_date": filing["reportDate"], "filing_date": filing["filingDate"],
            "accession": filing["accessionNumber"], "url": url, "years_in_order": years,
            "path": str(path.relative_to(ROOT)).replace("\\", "/"), "chars": len(text),
            "sha256": hashlib.sha256(raw).hexdigest(), "retrieved": datetime.now(timezone.utc).isoformat(),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=int, default=20, help="how many downloaded cases to process")
    args = parser.parse_args()
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))["files"]
    cases: dict[str, dict] = {}
    for f in manifest:
        cases.setdefault(f["aaer"], {"aaer": f["aaer"], "respondent": f["respondent"]})
    texts = {p.name.split("__")[0]: "" for p in (DATA / "text").glob("*.txt")}
    for p in (DATA / "text").glob("*.txt"):
        texts[p.name.split("__")[0]] += p.read_text(encoding="utf-8", errors="replace") + "\n"

    results: list[dict] = []
    done = 0
    for case in cases.values():
        if done >= args.cases:
            break
        print(f"{case['aaer']} {core_name(case['respondent'])[:50]}", file=sys.stderr)
        got = fetch_case(case, texts.get(case["aaer"], ""))
        results.extend(got)
        done += 1
    (DATA / "filings_manifest.json").write_text(json.dumps({"filings": results}, indent=1), encoding="utf-8")
    print(f"{len(results)} filings for {len({r['aaer'] for r in results})} of {done} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
