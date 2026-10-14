"""Record what the live web check returns for real companies, with the sources it cites.

    python -m benchmarks.review.web_live

Nothing here is simulated: it needs a real TAVILY_API_KEY and refuses to run without one. The output file is
the evidence a reviewer can open: each finding lists the public URLs it rests on and when it was retrieved.

Two groups are checked:
  known     real companies with well-documented public disputes. A working check should find them.
  fictional names that do not exist. A working check should find nothing, which tests for invented matters.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from packages.common.envfile import load_env_defaults
from packages.review.web import web_check

RESULTS = Path(__file__).with_name("web_live.json")

KNOWN = [
    ("Wirecard AG", "Payments group that collapsed in 2020 after EUR 1.9bn of cash was found not to exist"),
    ("Boeing Company", "737 MAX investigations, DOJ case, FAA penalties and securities litigation"),
    ("Theranos Inc", "Blood-testing startup; founder convicted of fraud"),
    ("Volkswagen AG", "Diesel emissions scandal, fines and settlements"),
    ("Enron Corporation", "Accounting fraud and bankruptcy, 2001"),
]
FICTIONAL = [
    ("Harlow Foods plc", "invented for the Fine Print examples"),
    ("Brightwell Components Ltd", "invented for the Fine Print examples"),
    ("Quillfeather Logistics GmbH", "invented for this check"),
]


def _document(names: List[str]) -> List[Dict[str, str]]:
    sentences = [f"{name} is a party to this agreement and will perform the services described in Schedule 1." for name in names]
    return [{"name": "agreement.txt", "text": " ".join(sentences + sentences)}]


async def check_group(label: str, group: List[tuple]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for name, note in group:
        out = await web_check(_document([name]), max_entities=1)
        lookup = out["lookups"][0] if out["lookups"] else {"results": 0, "relevant": 0, "error": "no entity extracted"}
        rows.append({
            "entity": name,
            "note": note,
            "results_returned": lookup["results"],
            "relevant_results": lookup["relevant"],
            "error": lookup["error"],
            "matter_found": bool(out["findings"]),
            "sources": [
                {"title": s["title"], "url": s["url"]}
                for f in out["findings"] for s in f["sources"]
            ],
        })
    return {"group": label, "entities": rows}


async def main() -> int:
    load_env_defaults()
    key = os.getenv("TAVILY_API_KEY", "")
    if not key or key.startswith("mock"):
        print("TAVILY_API_KEY is not set to a real key; refusing to produce simulated results.", file=sys.stderr)
        return 2
    known = await check_group("known", KNOWN)
    fictional = await check_group("fictional", FICTIONAL)
    found = sum(r["matter_found"] for r in known["entities"])
    invented = sum(r["matter_found"] for r in fictional["entities"])
    errors = sum(bool(r["error"]) for r in known["entities"] + fictional["entities"])
    out = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Tavily Search API, live",
        "summary": {
            "known_companies_with_matter_found": f"{found}/{len(KNOWN)}",
            "fictional_companies_with_matter_found": f"{invented}/{len(FICTIONAL)}",
            "lookup_errors": errors,
        },
        "caveats": [
            "A match means public sources mention the company with a dispute-related term. It is not a legal finding.",
            "Results can concern a different company with a similar name, and search results vary between calls.",
            "Fictional names test for invented matters. They are not a measure of precision on real, clean companies.",
        ],
        "groups": [known, fictional],
    }
    RESULTS.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out["summary"], indent=2))
    for row in known["entities"] + fictional["entities"]:
        mark = "MATTER" if row["matter_found"] else "none  "
        print(f'  {mark} {row["entity"]:28} {row["relevant_results"]}/{row["results_returned"]} relevant'
              + (f'  ERROR {row["error"]}' if row["error"] else ""))
        for s in row["sources"][:2]:
            print(f'           - {s["title"][:80]}\n             {s["url"]}')
    print(f"\nwrote {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
