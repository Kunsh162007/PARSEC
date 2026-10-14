"""Run "what this file still needs" on real document sets of different kinds, and keep the output for review.

There is no answer key for what a file "should" contain, so this is not a score. It records what the model asked for on
each real set, so a practitioner can judge the lists, and so changes to the prompt can be compared run to run.

    python -m benchmarks.requirements.samples [--samples 3]

Writes benchmarks/requirements/samples.json. Compare two such files with benchmarks/requirements/stability.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.fraud.fetch import ROOT
from packages.common.envfile import load_env_defaults

CUAD_ZIP = ROOT / "data" / "cuad" / "CUAD_v1.zip"
OUT = Path(__file__).with_name("samples.json")


def cuad_contract(pattern: str) -> dict:
    with zipfile.ZipFile(CUAD_ZIP) as z:
        names = sorted(n for n in z.namelist() if n.endswith(".txt") and "full_contract_txt" in n and re.search(pattern, n, re.I))
        name = names[0]
        return {"name": name.rsplit("/", 1)[-1], "text": z.read(name).decode("utf-8", "replace")}


def local(path: str, name: str = "") -> dict:
    p = ROOT / path
    return {"name": name or p.name, "text": p.read_text(encoding="utf-8")}


def first(pattern: str) -> str:
    return str(sorted((ROOT / "data").glob(pattern))[0].relative_to(ROOT)).replace("\\", "/")


def build_sets() -> list:
    release = first("ebitda/0000886206_*.txt")
    return [
        ("Licence agreement", [cuad_contract(r"LICENSE AGREEMENT")]),
        ("Franchise agreement", [cuad_contract(r"FRANCHISE")]),
        ("Endorsement agreement", [cuad_contract(r"ENDORSEMENT")]),
        ("Services agreement", [cuad_contract(r"SERVICES AGREEMENT")]),
        ("SEC enforcement order", [local("apps/review/examples/sec-order-latch.txt")]),
        ("Annual report (10-K)", [local("data/testpack/E_annual_report_alone/UPS_10-K_2020.txt")]),
        ("Earnings release", [local(release, "earnings-release.txt")]),
        ("Small data room: contract, order and release", [
            local("apps/review/examples/accuray-distributor-agreement.txt"),
            local("apps/review/examples/sec-order-latch.txt"), local(release, "earnings-release.txt")]),
    ]


async def main() -> int:
    try:  # a Windows cp1252 console cannot print the model's non-ASCII hyphens and would crash before the file is written
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # stdout is not a reconfigurable text stream; printing may still fail, but nothing better can be done here
    load_env_defaults()
    from packages.gateway.llm import LLMGateway
    from packages.review.requirements import determine_requirements

    if not CUAD_ZIP.exists():
        print("CUAD not found. Run: python -m benchmarks.cuad.dataset --fetch", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=int, default=None,
                        help="model calls per set, merged by majority (default: determine_requirements' default)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="override the route's temperature for this run only (in memory; config is not changed)")
    parser.add_argument("--out", default=str(OUT), help="where to write the results")
    args = parser.parse_args()
    gateway = LLMGateway()
    if args.temperature is not None:
        from packages.review.requirements import TASK
        gateway.routing[TASK] = {**gateway.routing[TASK], "temperature": args.temperature}
    sets = build_sets()
    extra = {} if args.samples is None else {"samples": args.samples}
    outs = await asyncio.gather(*(determine_requirements(gateway, docs, **extra) for _, docs in sets))
    rows = []
    for (label, docs), out in zip(sets, outs):
        rows.append({"name": label, "documents": [d["name"] for d in docs], "available": out["available"],
                     "reason": out.get("reason", ""), "engagement": out.get("engagement", ""),
                     "items": out.get("items", []), "cost_usd": out.get("cost_usd", 0.0),
                     "samples": out.get("samples", 1), "samples_ok": out.get("samples_ok", int(out["available"]))})
        print(f"\n== {label} | {out.get('engagement', out.get('reason', ''))[:110]}")
        for i in out.get("items", [])[:6]:
            tag = "" if i["name_known"] else "  [described]"
            agreed = f" ({i['agreement']}/{i['of_runs']})" if "agreement" in i else ""
            print(f"   - [{i['priority']}/{i['status']}] {i['name']}{tag}{agreed}")
    out_path = Path(args.out)
    out_path.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "note": "Model output on real document sets, kept for human review. Not a score.",
                                    "samples": args.samples or 1, "temperature_override": args.temperature,
                                    "sets": rows}, indent=2), encoding="utf-8")
    print(f"\nwritten to {out_path}; cost ${sum(r['cost_usd'] for r in rows):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
