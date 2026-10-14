"""Can PARSEC read real Adjusted EBITDA reconciliations? Checked by arithmetic, not by a hand-made answer key.

    python -m benchmarks.ebitda.fetch --companies 40     # once: real earnings releases from SEC EDGAR
    python -m benchmarks.ebitda.run --split dev          # tune here
    python -m benchmarks.ebitda.run --split test         # report this, once

A release passes only if every line the model returns is found in the document with its value, and the lines add up
to the company's own stated Adjusted EBITDA within rounding. A wrong reading almost never adds up by accident.
Writes benchmarks/ebitda/results_<split>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.fraud.fetch import ROOT
from packages.common.envfile import load_env_defaults
from packages.evidence.extractor import DocumentExtractionWorker

DATA = ROOT / "data" / "ebitda"
OUT = Path(__file__).parent
CONCURRENCY = 4


async def main(args: argparse.Namespace) -> int:
    load_env_defaults()
    from packages.gateway.llm import LLMGateway
    from packages.workflow.bridge import extract_bridge

    manifest = DATA / args.set
    if not manifest.exists():
        print("No releases. Run: python -m benchmarks.ebitda.fetch", file=sys.stderr)
        return 2
    releases = [r for r in json.loads(manifest.read_text(encoding="utf-8"))["releases"] if r["split"] == args.split]
    gateway = LLMGateway()
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(r):
        text = (ROOT / r["path"]).read_text(encoding="utf-8")
        old_base, _ = DocumentExtractionWorker.extract_base_ebitda(text)
        async with sem:
            out = await extract_bridge(gateway, text)
        row = {"company": r["company"], "url": r["url"], "status": out["status"], "reason": out.get("reason", ""),
               "previous_pattern_found_base": str(old_base) != "0.00", "cost_usd": out.get("cost_usd", 0.0)}
        if out.get("total") and out["total"].get("ok"):
            row.update({"period": out.get("period"), "units": out.get("units"),
                        "start": f"{out['start'].get('label')} {out['start'].get('value')}",
                        "adjustments": len(out.get("adjustments", [])),
                        "stated_adjusted_ebitda": str(out["total"]["value"]),
                        "computed": str(out.get("computed_total", "")), "difference": str(out.get("difference", ""))})
        if out.get("unverified"):
            row["unverified"] = out["unverified"]
        print(f"{row['status']:14s} {r['company'][:45]}", file=sys.stderr)
        return row

    rows = await asyncio.gather(*(one(r) for r in releases))
    counts = Counter(r["status"] for r in rows)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "split": args.split, "set": args.set, "releases": len(rows),
        "source": "8-K earnings releases from SEC EDGAR full-text search, one per company",
        "counts": {k: counts.get(k, 0) for k in ("foots", "does_not_foot", "incomplete", "unavailable")},
        "previous_pattern_found_base": sum(r["previous_pattern_found_base"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "rows": rows,
    }
    path = OUT / (f"results_{args.split}.json" if args.set == "manifest.json" else f"results_{args.set.replace('.json', '')}_{args.split}.json")
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("split", "releases", "counts", "previous_pattern_found_base", "cost_usd")}, indent=1))
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--set", default="manifest.json", help="which fetched set to score")
    sys.exit(asyncio.run(main(parser.parse_args())))
