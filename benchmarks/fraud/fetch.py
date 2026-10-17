"""Download real SEC accounting-enforcement documents (AAERs) for companies the SEC found to have committed fraud.

Source: https://www.sec.gov/enforcement-litigation/accounting-auditing-enforcement-releases
These are US government documents in the public domain. Nothing here is generated or simulated.

    python -m benchmarks.fraud.fetch --index                 # crawl the release index -> data/fraud/aaer_index.json
    python -m benchmarks.fraud.fetch --download 60           # fetch the primary documents of 60 issuer cases
    python -m benchmarks.fraud.fetch --download 60 --since 2012

Output layout (git-ignored):
    data/fraud/aaer_index.json                 every release: date, respondent, release numbers, links
    data/fraud/aaer/AAER-4604/<file>           the order / complaint, as published by the SEC
    data/fraud/manifest.json                   what was fetched, when, from where, sha256 of each file
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "fraud"
BASE = "https://www.sec.gov"
INDEX_URL = BASE + "/enforcement-litigation/accounting-auditing-enforcement-releases"
# The SEC asks automated clients to name a contact address in the User-Agent.
USER_AGENT = f"FinePrintResearch/1.0 (contact: {os.getenv('SEC_CONTACT_EMAIL', 'research@example.com')})"
DELAY_SECONDS = 0.25  # SEC fair-access policy allows at most 10 requests per second
ISSUER_WORDS = re.compile(
    r"\b(Inc|Corp|Corporation|Co|Company|Ltd|Limited|LLC|L\.P|LP|Holdings|Group|plc|N\.V|S\.A|AG|Technologies|"
    r"Systems|Industries|International|Bancorp|Bank|Pharmaceuticals|Energy|Entertainment)\b\.?",
    re.IGNORECASE,
)
PERSON_WORDS = re.compile(r"\b(CPA|CFO|CEO|Esq|Jr|Sr)\b|,\s*(CPA|CFA)")


def http_get(url: str, retries: int = 3) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
            time.sleep(DELAY_SECONDS)
            return data
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"could not fetch {url}: {last_error}")


def parse_rows(page: str) -> list[dict]:
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S):
        date = re.search(r'<time[^>]*datetime="([^"]+)"', row)
        title = re.search(r"release-view__respondents'>(.*?)</div>", row, flags=re.S)
        number = re.search(r"view-table_subfield_release_number.*?subfield_value\">(.*?)</span>", row, flags=re.S)
        if not (date and title and number):
            continue
        primary = re.search(r"href='([^']+)'", title.group(1)) or re.search(r'href="([^"]+)"', title.group(1))
        names = html.unescape(re.sub(r"<[^>]+>", " ", title.group(1)))
        see_also = re.search(r"subfield_see_also(.*)", row, flags=re.S)
        extra = re.findall(r'href="([^"]+)"', see_also.group(1)) if see_also else []
        numbers = re.sub(r"\s+", " ", html.unescape(number.group(1))).strip()
        aaer = re.search(r"AAER-(\d+)", numbers)
        rows.append({
            "date": date.group(1)[:10],
            "respondent": re.sub(r"\s+", " ", names).strip(),
            "release_numbers": numbers,
            "aaer": f"AAER-{aaer.group(1)}" if aaer else None,
            "primary_url": urljoin(BASE, primary.group(1)) if primary else None,
            "related_urls": [urljoin(BASE, link) for link in extra],
        })
    return rows


def build_index() -> list[dict]:
    DATA.mkdir(parents=True, exist_ok=True)
    first = http_get(INDEX_URL).decode("utf-8", "replace")
    last_page = max([int(n) for n in re.findall(r"\?page=(\d+)", first)] or [0])
    releases = parse_rows(first)
    for number in range(1, last_page + 1):
        page = http_get(f"{INDEX_URL}?page={number}").decode("utf-8", "replace")
        releases.extend(parse_rows(page))
        print(f"index page {number}/{last_page}: {len(releases)} releases", file=sys.stderr)
    seen: set[str] = set()
    unique = []
    for release in releases:
        key = release["aaer"] or release["release_numbers"] + release["respondent"]
        if key not in seen:
            seen.add(key)
            unique.append(release)
    (DATA / "aaer_index.json").write_text(
        json.dumps({"source": INDEX_URL, "retrieved": datetime.now(timezone.utc).isoformat(), "releases": unique}, indent=1),
        encoding="utf-8",
    )
    return unique


def looks_like_issuer(respondent: str) -> bool:
    return bool(ISSUER_WORDS.search(respondent)) and not PERSON_WORDS.search(respondent)


def safe_name(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", url.rsplit("/", 1)[-1] or "document") or "document"


def download(limit: int, since: int) -> list[dict]:
    index_path = DATA / "aaer_index.json"
    if not index_path.exists():
        build_index()
    releases = json.loads(index_path.read_text(encoding="utf-8"))["releases"]
    chosen = [r for r in releases if r["aaer"] and r["primary_url"] and int(r["date"][:4]) >= since and looks_like_issuer(r["respondent"])]
    manifest_path = DATA / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"files": []}
    done = {f["aaer"] for f in manifest["files"]}
    fetched = 0
    for release in chosen:
        if fetched >= limit:
            break
        if release["aaer"] in done:
            continue
        urls = [release["primary_url"], *[u for u in release["related_urls"] if u.lower().endswith(".pdf")]]
        target = DATA / "aaer" / release["aaer"]
        target.mkdir(parents=True, exist_ok=True)
        for url in urls:
            try:
                body = http_get(url)
            except RuntimeError as error:
                print(f"skip {url}: {error}", file=sys.stderr)
                continue
            path = target / safe_name(url)
            path.write_bytes(body)
            manifest["files"].append({
                "aaer": release["aaer"], "respondent": release["respondent"], "date": release["date"],
                "url": url, "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "retrieved": datetime.now(timezone.utc).isoformat(),
            })
        fetched += 1
        print(f"{release['aaer']} {release['respondent'][:60]}", file=sys.stderr)
    manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest["files"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--index", action="store_true", help="crawl the release index")
    parser.add_argument("--download", type=int, metavar="N", help="download documents for N issuer cases")
    parser.add_argument("--since", type=int, default=2005, help="earliest release year (default 2005)")
    args = parser.parse_args()
    if args.index:
        print(f"{len(build_index())} releases indexed")
    if args.download:
        print(f"{len(download(args.download, args.since))} files in manifest")
    if not (args.index or args.download):
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
