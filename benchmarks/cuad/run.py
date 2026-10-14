"""Benchmark Fine Print on CUAD: real contracts, lawyer-labeled clauses.

    python -m benchmarks.cuad.dataset --fetch            # once
    python -m benchmarks.cuad.run                        # rules on all 510 contracts, free
    python -m benchmarks.cuad.run --ai 40                # plus the Nebius model on 40 contracts (costs a few dollars)
    python -m benchmarks.cuad.run --limit 100 --ai 20    # smaller and faster

Writes benchmarks/cuad/results.json, which GET /api/arena/benchmark serves.
"""
import argparse
import asyncio
import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from benchmarks.cuad.dataset import DEFAULT_ZIP, load_contracts, verify_offsets
from benchmarks.cuad.mapping import MAPPED_CATEGORIES
from benchmarks.cuad.score import aggregate, score_contract

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "benchmarks" / "review" / "rules_driver.js"
RESULTS = Path(__file__).with_name("results.json")
RULE_BATCH = 20
AI_CONCURRENCY = 4

CITATION = ("Hendrycks, Burns, Chen, Ball. CUAD: An Expert-Annotated NLP Dataset for Legal Contract Review. "
            "NeurIPS 2021. The Atticus Project, CC BY 4.0. DOI 10.5281/zenodo.4595826.")


def run_rules(contracts: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    """The real Fine Print engine, all spans, one contract at a time in batches."""
    found: Dict[int, List[Dict[str, Any]]] = {}
    for start in range(0, len(contracts), RULE_BATCH):
        batch = [{"id": str(i), "text": contracts[i]["text"]} for i in range(start, min(start + RULE_BATCH, len(contracts)))]
        proc = subprocess.run(["node", str(DRIVER)], input=json.dumps(batch), capture_output=True, text=True,
                              encoding="utf-8", cwd=ROOT, check=True)
        for key, findings in json.loads(proc.stdout).items():
            found[int(key)] = findings
    return found


SPLIT_SEED = 20241012


def split_indices(contracts: List[Dict[str, Any]], split: str) -> List[int]:
    """A fixed 50/50 dev/test split. Rules are tuned on dev only; test is for reporting.

    Deterministic: contracts are ordered by title, then shuffled with a constant seed, so the halves never change.
    """
    order = sorted(range(len(contracts)), key=lambda i: contracts[i]["title"])
    random.Random(SPLIT_SEED).shuffle(order)
    half = len(order) // 2
    parts = {"dev": sorted(order[:half]), "test": sorted(order[half:]), "all": list(range(len(contracts)))}
    return parts[split]


def label_coverage(contracts: List[Dict[str, Any]]) -> float:
    """Share of contract characters inside a labeled span of a mapped category: the precision of highlighting at random."""
    covered = total = 0
    for c in contracts:
        mask = bytearray(len(c["text"]))
        for category, spans in c["labels"].items():
            if category in MAPPED_CATEGORIES:
                for s, e in spans:
                    mask[s:e] = b"\x01" * (e - s)
        covered += sum(mask)
        total += len(mask)
    return round(covered / total, 4) if total else 0.0


def _overlap(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


async def run_ai(contracts: List[Dict[str, Any]], indices: List[int]) -> Dict[str, Any]:
    from packages.gateway.llm import LLMGateway
    from packages.review.deep import CHUNK_CHARS, CHUNK_OVERLAP, MAX_CHUNKS, DeepReviewUnavailable, chunk_text, deep_review

    gateway = LLMGateway()
    sem = asyncio.Semaphore(AI_CONCURRENCY)
    per_contract: Dict[int, Dict[str, Any]] = {}
    totals = {"tokens": 0, "cost_usd": 0.0, "errors": 0, "truncated_contracts": 0, "failed_chunks": 0}

    async def one(i: int) -> None:
        async with sem:
            try:
                out = await deep_review(gateway, [{"name": f"c{i}", "text": contracts[i]["text"]}], [],
                                        use_triage=False, use_adjudication=False)
            except DeepReviewUnavailable:
                totals["errors"] += 1
                return
        pieces = chunk_text(contracts[i]["text"], CHUNK_CHARS, CHUNK_OVERLAP)[:MAX_CHUNKS]
        covered_to = (pieces[-1][0] + len(pieces[-1][1])) if pieces else 0
        totals["tokens"] += out["tokens"]
        totals["cost_usd"] += out["cost_usd"]
        totals["truncated_contracts"] += int(out["truncated"])
        totals["failed_chunks"] += out.get("failed_chunks", 0)
        per_contract[i] = {"findings": out["findings"], "covered_to": covered_to, "dropped": out["dropped"]}

    await asyncio.gather(*(one(i) for i in indices))
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    return {"per_contract": per_contract, "totals": totals, "models": {
        "super": gateway.route_task("document_review_json")["model"]}}


def score_ai(contracts, ai) -> Dict[str, Any]:
    """Lens-agnostic: does any AI finding overlap a labeled clause? Labels past the covered window are excluded."""
    cats: Dict[str, Dict[str, int]] = {}
    findings_total = on_mapped = on_any = 0
    excluded = 0
    for i, row in ai["per_contract"].items():
        c = contracts[i]
        spans = [(f["spans"][0]["start"], f["spans"][0]["end"]) for f in row["findings"] if f.get("spans")]
        all_labels = [s for v in c["labels"].values() for s in v]
        mapped_labels = [s for k, v in c["labels"].items() if k in MAPPED_CATEGORIES for s in v]
        findings_total += len(spans)
        on_mapped += sum(any(_overlap(s, l) for l in mapped_labels) for s in spans)
        on_any += sum(any(_overlap(s, l) for l in all_labels) for s in spans)
        for category, label_spans in c["labels"].items():
            if category not in MAPPED_CATEGORIES or not label_spans:
                continue
            reachable = [l for l in label_spans if l[1] <= row["covered_to"]]
            excluded += len(label_spans) - len(reachable)
            if not reachable:
                continue
            slot = cats.setdefault(category, {"contracts_with_clause": 0, "detected": 0})
            slot["contracts_with_clause"] += 1
            slot["detected"] += int(any(_overlap(s, l) for s in spans for l in reachable))
    return {
        "contracts_scored": len(ai["per_contract"]),
        "findings": findings_total,
        "findings_per_contract": round(findings_total / max(1, len(ai["per_contract"])), 2),
        "precision_mapped_labels": round(on_mapped / findings_total, 3) if findings_total else None,
        "precision_any_label": round(on_any / findings_total, 3) if findings_total else None,
        "labeled_spans_excluded_beyond_read_window": excluded,
        "categories": {c: {**v, "recall": round(v["detected"] / v["contracts_with_clause"], 3)}
                       for c, v in sorted(cats.items())},
    }


async def main(args: argparse.Namespace) -> int:
    if not DEFAULT_ZIP.exists():
        print("Dataset not found. Run: python -m benchmarks.cuad.dataset --fetch", file=sys.stderr)
        return 2
    contracts = load_contracts(DEFAULT_ZIP)
    bad = verify_offsets(contracts)
    rng = random.Random(args.seed)
    indices = split_indices(contracts, args.split)
    if args.limit:
        indices = sorted(rng.sample(indices, min(args.limit, len(indices))))
    chosen = [contracts[i] for i in indices]

    print(f"scoring rules on {len(chosen)} contracts ...", flush=True)
    found = run_rules(chosen)
    rows = [score_contract({k: v for k, v in c["labels"].items()}, found.get(n, [])) for n, c in enumerate(chosen)]
    out: Dict[str, Any] = {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": {
            "name": "CUAD v1 (Contract Understanding Atticus Dataset)",
            "origin": "510 real commercial contracts from SEC EDGAR, clauses labeled by lawyers",
            "license": "CC BY 4.0", "citation": CITATION,
            "contracts_total": len(contracts), "contracts_scored": len(chosen), "split": args.split,
            "label_offsets_not_matching_text": bad,
            "sample_seed": args.seed if args.limit else None,
        },
        "baseline": {
            "share_of_text_inside_a_mapped_clause": label_coverage(chosen),
            "meaning": "A rule that highlighted text at random would have about this precision.",
        },
        "rules": aggregate(rows),
        "notes": [
            "Recall: of contracts where a lawyer labeled a clause category, how many had a finding overlapping it.",
            "Precision is a lower bound: a clause the labelers did not mark counts as a miss here.",
            "Only categories Fine Print has rules for are scored; the rest of CUAD's 41 categories are not.",
        ],
    }

    if args.ai:
        eligible = [n for n, c in enumerate(chosen) if any(c["labels"].get(k) for k in MAPPED_CATEGORIES)]
        sample = sorted(rng.sample(eligible, min(args.ai, len(eligible))))
        print(f"running the model on {len(sample)} contracts ...", flush=True)
        ai = await run_ai(chosen, sample)
        out["ai"] = {"ran": True, "models": ai["models"], "usage": ai["totals"], "score": score_ai(chosen, ai)}
        combined: Dict[str, Dict[str, int]] = {}
        for n in sample:
            c = chosen[n]
            spans = [(f["spans"][0]["start"], f["spans"][0]["end"]) for f in ai["per_contract"].get(n, {}).get("findings", [])
                     if f.get("spans")]
            covered_to = ai["per_contract"].get(n, {}).get("covered_to", 0)
            for category, label_spans in c["labels"].items():
                if category not in MAPPED_CATEGORIES or not label_spans:
                    continue
                reachable = [l for l in label_spans if l[1] <= covered_to]
                if not reachable:
                    continue
                rule_hit = rows[n]["categories"].get(category, {}).get("detected", False)
                ai_hit = any(_overlap(s, l) for s in spans for l in reachable)
                slot = combined.setdefault(category, {"contracts_with_clause": 0, "rules": 0, "model": 0, "either": 0})
                slot["contracts_with_clause"] += 1
                slot["rules"] += int(rule_hit)
                slot["model"] += int(ai_hit)
                slot["either"] += int(rule_hit or ai_hit)
        out["ai"]["rules_vs_model_vs_either"] = {
            c: {**v, "either_recall": round(v["either"] / v["contracts_with_clause"], 3)} for c, v in sorted(combined.items())
        }
    else:
        out["ai"] = {"ran": False, "reason": "not requested (use --ai N)"}

    target = Path(args.out) if args.out else RESULTS
    target.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {target}\n")
    print(f"chance precision (share of text inside a mapped clause): {out['baseline']['share_of_text_inside_a_mapped_clause']}")
    print(f"{'category':32} {'clause in':>9} {'rules found':>12} {'recall':>7}")
    for c, v in out["rules"]["categories"].items():
        print(f"{c:32} {v['contracts_with_clause']:>9} {v['detected']:>12} {str(v['recall']):>7}")
    print(f"\n{'rule':18} {'hits':>7} {'on mapped clause':>17} {'precision':>10}")
    for r, v in out["rules"]["rules"].items():
        print(f"{r:18} {v['hits']:>7} {v['on_label']:>17} {str(v['precision']):>10}")
    if args.ai and out["ai"].get("ran"):
        s = out["ai"]["score"]
        print(f"\nmodel on {s['contracts_scored']} contracts: {s['findings_per_contract']} findings each, "
              f"precision vs mapped labels {s['precision_mapped_labels']}, vs any label {s['precision_any_label']}, "
              f"${out['ai']['usage']['cost_usd']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="score a random sample of N contracts (default: all)")
    parser.add_argument("--ai", type=int, default=0, help="also run the Nebius model on N contracts")
    parser.add_argument("--seed", type=int, default=7, help="sampling seed")
    parser.add_argument("--split", choices=("dev", "test", "all"), default="all",
                        help="dev: used to tune rules. test: held out, report this. all: everything.")
    parser.add_argument("--out", default="", help="results path (default: benchmarks/cuad/results.json)")
    sys.exit(asyncio.run(main(parser.parse_args())))
