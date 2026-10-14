"""Measure "thin, then verify with a citation" against the rules on CUAD, category by category.

    python -m benchmarks.cuad.verify_run --split dev --n 25        # tune here
    python -m benchmarks.cuad.verify_run --split test --n 30       # report this, once

Scoring is category-aware for the model: a passage the model tags "most_favored_nation" counts only if it overlaps a
clause the lawyers labelled "Most Favored Nation". Rules are scored through the existing rule-to-category mapping.
Writes benchmarks/cuad/history/04_verify_<split>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from benchmarks.cuad.dataset import DEFAULT_ZIP, load_contracts
from benchmarks.cuad.mapping import MAPPED_CATEGORIES, RULE_TO_CATEGORIES
from benchmarks.cuad.run import run_rules, split_indices

HISTORY = Path(__file__).with_name("history")
CATEGORY_TO_CUAD = {
    "change_of_control": "Change Of Control", "anti_assignment": "Anti-Assignment",
    "termination_for_convenience": "Termination For Convenience", "exclusivity": "Exclusivity",
    "non_compete": "Non-Compete", "no_solicit_employees": "No-Solicit Of Employees",
    "no_solicit_customers": "No-Solicit Of Customers", "rofr": "Rofr/Rofo/Rofn",
    "most_favored_nation": "Most Favored Nation", "renewal_term": "Renewal Term",
    "uncapped_liability": "Uncapped Liability", "liquidated_damages": "Liquidated Damages",
    "minimum_commitment": "Minimum Commitment", "ip_ownership_assignment": "Ip Ownership Assignment",
    "joint_ip_ownership": "Joint Ip Ownership",
}
CONCURRENT_CONTRACTS = 3


def _overlap(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def rule_spans_by_category(findings: List[Dict[str, Any]]) -> Dict[str, List[tuple]]:
    out: Dict[str, List[tuple]] = {}
    for f in findings:
        for cat in RULE_TO_CATEGORIES.get(f["id"], []):
            out.setdefault(cat, []).extend((s["start"], s["end"]) for s in f.get("spans", []))
    return out


def model_spans_by_category(hits: List[Dict[str, Any]]) -> Dict[str, List[tuple]]:
    out: Dict[str, List[tuple]] = {}
    for h in hits:
        out.setdefault(CATEGORY_TO_CUAD[h["category"]], []).append((h["start"], h["end"]))
    return out


def score(contracts, sample, rules, model) -> Dict[str, Any]:
    cats: Dict[str, Dict[str, int]] = {}
    hits_total = hits_on_own_category = hits_on_any_mapped = 0
    for n in sample:
        c = contracts[n]
        r = rule_spans_by_category(rules.get(n, []))
        m = model_spans_by_category(model[n]["hits"])
        mapped = [l for k, v in c["labels"].items() if k in MAPPED_CATEGORIES for l in v]
        for cat, spans in m.items():
            own = c["labels"].get(cat, [])
            hits_total += len(spans)
            hits_on_own_category += sum(any(_overlap(s, l) for l in own) for s in spans)
            hits_on_any_mapped += sum(any(_overlap(s, l) for l in mapped) for s in spans)
        for cat, labels in c["labels"].items():
            if cat not in MAPPED_CATEGORIES or not labels:
                continue
            slot = cats.setdefault(cat, {"contracts_with_clause": 0, "rules": 0, "model": 0, "either": 0})
            got_r = any(_overlap(s, l) for s in r.get(cat, []) for l in labels)
            got_m = any(_overlap(s, l) for s in m.get(cat, []) for l in labels)
            slot["contracts_with_clause"] += 1
            slot["rules"] += got_r
            slot["model"] += got_m
            slot["either"] += got_r or got_m
    pooled = {k: sum(v[k] for v in cats.values()) for k in ("contracts_with_clause", "rules", "model", "either")}
    rate = lambda a, b: round(a / b, 3) if b else None
    return {
        "pooled_recall": {k: rate(pooled[k], pooled["contracts_with_clause"]) for k in ("rules", "model", "either")},
        "labelled_clauses": pooled["contracts_with_clause"],
        "model_hits": hits_total,
        "model_precision_same_category": rate(hits_on_own_category, hits_total),
        "model_precision_any_mapped_label": rate(hits_on_any_mapped, hits_total),
        "categories": {k: {**v, **{f"recall_{x}": rate(v[x], v["contracts_with_clause"]) for x in ("rules", "model", "either")}}
                       for k, v in sorted(cats.items())},
    }


def sample_pool(contracts, split: str) -> List[int]:
    """Contracts of the split that carry at least one label in a mapped category."""
    return [i for i in split_indices(contracts, split) if any(contracts[i]["labels"].get(k) for k in MAPPED_CATEGORIES)]


def draw_sample(pool: List[int], seed: int, n: int) -> List[int]:
    """The same draw the earlier runs used, so a seed always names the same contracts for the same pool."""
    return sorted(random.Random(seed).sample(pool, min(n, len(pool))))


def resolve_prompt_options(args: argparse.Namespace) -> tuple:
    """The shipped defaults from clauses.py, unless --plain-prompt turns both off or a flag forces one on."""
    from packages.review import clauses

    if args.plain_prompt:
        return bool(args.no_think), bool(args.near_miss)
    return bool(args.no_think or clauses.USE_NO_THINK), bool(args.near_miss or clauses.USE_NEAR_MISS)


def resolve_round7_options(args: argparse.Namespace) -> tuple:
    """(official definitions, votes): the clauses.py defaults unless --plain-prompt, which leaves only explicit flags."""
    from packages.review import clauses

    official = bool(getattr(args, "official_definitions", False))
    votes = getattr(args, "votes", None)
    if args.plain_prompt:
        return official, int(votes or 1)
    default_votes = clauses.VOTES if clauses.USE_VOTING else 1
    return official or clauses.USE_OFFICIAL_DEFINITIONS, int(votes or default_votes)


def exclude_samples(pool: List[int], seeds: List[int], n: int) -> tuple:
    """Remove earlier samples in the order they were drawn: each seed draws from the pool left by the seeds before it.

    This reproduces a chain of held-out samples: seed 23 was drawn after removing the seed-11 sample, and so on.
    """
    excluded: List[int] = []
    for seed in seeds:
        drawn = draw_sample(pool, seed, n)
        excluded.extend(drawn)
        pool = [i for i in pool if i not in set(drawn)]
    return pool, sorted(excluded)


def output_path(args: argparse.Namespace) -> Path:
    if args.out:
        path = HISTORY / args.out
        if path.exists():
            raise SystemExit(f"{path} already exists; history files are never overwritten by --out")
        return path
    return HISTORY / (f"05_confirm_{args.split}.json" if args.confirm else f"04_verify_{args.split}.json")


def _hit_key(h: Dict[str, Any]) -> tuple:
    return (h["category"], h["start"], h["end"])


def hit_details(contracts, sample, model, index_by_local: Dict[int, int]) -> List[Dict[str, Any]]:
    """One row per model hit: was it on a label of its own category, of any mapped category, and the gate's verdict."""
    rows = []
    for n in sample:
        c = contracts[n]
        mapped = [l for k, v in c["labels"].items() if k in MAPPED_CATEGORIES for l in v]
        gated = model[n].get("gate")
        verdicts = {_hit_key(h): h.get("gate", "yes") for h in gated["hits"]} if gated else {}
        for h in model[n]["hits"]:
            own = c["labels"].get(CATEGORY_TO_CUAD[h["category"]], [])
            span = (h["start"], h["end"])
            row = {"contract": index_by_local[n], "title": c["title"], "category": h["category"], "quote": h["quote"],
                   "start": h["start"], "end": h["end"],
                   "on_own_category": any(_overlap(span, l) for l in own),
                   "on_any_mapped_label": any(_overlap(span, l) for l in mapped)}
            if gated:
                row["gate"] = verdicts.get(_hit_key(h), "no")
            rows.append(row)
    return rows


async def run_model(contracts, sample, confirm: bool = False, gate: bool = False, no_think: bool = False,
                    near_miss: bool = False, official: bool = False, votes: int = 1) -> Dict[int, Dict[str, Any]]:
    from packages.gateway.llm import LLMGateway
    from packages.review.clauses import confirm_hits, gate_hits, verify_clauses

    gateway = LLMGateway()
    sem = asyncio.Semaphore(CONCURRENT_CONTRACTS)
    out: Dict[int, Dict[str, Any]] = {}

    async def one(n: int) -> None:
        async with sem:
            out[n] = await verify_clauses(gateway, contracts[n]["text"], no_think=no_think, near_miss=near_miss,
                                          official=official, votes=votes)
            if confirm:
                out[n]["confirmation"] = await confirm_hits(gateway, contracts[n]["text"], out[n]["hits"])
            if gate:
                out[n]["gate"] = await gate_hits(gateway, contracts[n]["text"], out[n]["hits"])
            print(f"  contract {n}: {out[n]['candidates']} candidates, {len(out[n]['hits'])} verified hits, "
                  f"{out[n]['dropped']} dropped, {out[n]['failed_calls']} failed calls", file=sys.stderr)

    await asyncio.gather(*(one(n) for n in sample))
    return out


async def main(args: argparse.Namespace) -> int:
    if not DEFAULT_ZIP.exists():
        print("Dataset not found. Run: python -m benchmarks.cuad.dataset --fetch", file=sys.stderr)
        return 2
    path = output_path(args)  # checked before spending anything
    contracts = load_contracts(DEFAULT_ZIP)
    pool = sample_pool(contracts, args.split)
    exclude_seeds = list(args.exclude_seed or [])
    pool, excluded = exclude_samples(pool, exclude_seeds, args.exclude_n)
    sample = draw_sample(pool, args.seed, args.n)
    chosen = [contracts[i] for i in sample]
    local = list(range(len(chosen)))

    print(f"rules on {len(chosen)} contracts ...", file=sys.stderr)
    rules = run_rules(chosen)
    print("model on the same contracts ...", file=sys.stderr)
    no_think, near_miss = resolve_prompt_options(args)
    official, votes = resolve_round7_options(args)
    model = await run_model(chosen, local, confirm=args.confirm, gate=args.gate, no_think=no_think,
                            near_miss=near_miss, official=official, votes=votes)

    usage = {k: sum(model[n][k] for n in local) for k in ("passages", "candidates", "calls", "failed_calls", "dropped", "tokens")}
    usage["cost_usd"] = round(sum(model[n]["cost_usd"] for n in local), 4)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "broad net selects candidate passages; Nemotron Super classifies them; each hit must quote its passage",
        "split": args.split, "contracts": len(chosen), "seed": args.seed,
        "variant": {"no_think": no_think, "near_miss": near_miss, "gate": args.gate,
                    "official_definitions": official, "votes": votes},
        "sample": sample, "usage": usage,
        "score": score(chosen, local, rules, model),
    }
    if official:
        from packages.review.clauses import OFFICIAL_SOURCE
        result["definitions_source"] = OFFICIAL_SOURCE
    if votes > 1:
        from packages.review.clauses import MIN_VOTES, VOTE_TASK
        result["voting"] = {"votes": votes, "min_votes": min(MIN_VOTES, votes), "task": VOTE_TASK,
                            "rule": "keep a hit when that many passes tag an overlapping span with the same category"}
        # Each sampled pass scored on its own, for the variance between single runs at the voting temperature.
        result["pass_scores"] = [
            {k: v for k, v in score(chosen, local, rules, {n: {"hits": model[n]["pass_hits"][p]} for n in local}).items()
             if k != "categories"}
            for p in range(votes)]
    if exclude_seeds:
        result["excluded"] = {"seeds": exclude_seeds, "n": args.exclude_n, "contracts": excluded,
                              "overlap_with_sample": sorted(set(excluded) & set(sample))}
    if args.gate:
        gated = {n: {**model[n], "hits": model[n]["gate"]["hits"]} for n in local}
        result["gate"] = {
            "model": "Nano answers yes/no per Super tag; tags it could not check are kept",
            "rejected": sum(model[n]["gate"]["rejected"] for n in local),
            "unchecked": sum(model[n]["gate"]["unchecked"] for n in local),
            "failed_calls": sum(model[n]["gate"]["failed_calls"] for n in local),
            "cost_usd": round(sum(model[n]["gate"]["cost_usd"] for n in local), 4),
            "score": score(chosen, local, rules, gated),
        }
    if args.details:
        rows = hit_details(chosen, local, model, dict(zip(local, sample)))
        Path(args.details).write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"hit details written to {args.details}", file=sys.stderr)
    if args.confirm:
        confirmed = {n: {**model[n], "hits": model[n]["confirmation"]["hits"]} for n in local}
        result["confirmation"] = {
            "model": "Ultra checks each Super tag; tags without a verdict are kept",
            "rejected": sum(model[n]["confirmation"]["rejected"] for n in local),
            "unconfirmed": sum(model[n]["confirmation"]["unconfirmed"] for n in local),
            "failed_calls": sum(model[n]["confirmation"]["failed_calls"] for n in local),
            "cost_usd": round(sum(model[n]["confirmation"]["cost_usd"] for n in local), 4),
            "score": score(chosen, local, rules, confirmed),
        }
    HISTORY.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    s = result["score"]
    print(json.dumps({"pooled_recall": s["pooled_recall"], "labelled_clauses": s["labelled_clauses"],
                      "model_precision_same_category": s["model_precision_same_category"],
                      "model_precision_any_mapped_label": s["model_precision_any_mapped_label"], "usage": usage}, indent=1))
    for cat, v in s["categories"].items():
        print(f"{cat:28s} n={v['contracts_with_clause']:3d} rules {v['recall_rules']:.2f} model {v['recall_model']:.2f} either {v['recall_either']:.2f}")
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--n", type=int, default=25, help="contracts to sample from the split")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--confirm", action="store_true", help="also have Ultra check each tag (writes 05_confirm_<split>.json)")
    parser.add_argument("--gate", action="store_true", help="also have Nano answer yes/no per tag; scored separately")
    parser.add_argument("--no-think", action="store_true", help='prefix "/no_think" to the classification prompt')
    parser.add_argument("--near-miss", action="store_true", help='add the "Not this:" near-miss notes to the prompt')
    parser.add_argument("--plain-prompt", action="store_true",
                        help="ignore the clauses.py prompt defaults (the pre-06 prompt); --no-think/--near-miss still apply")
    parser.add_argument("--details", help="also write one row per model hit (category, quote, label overlap) to this path")
    parser.add_argument("--official-definitions", action="store_true",
                        help="define each category with CUAD's own labelling instruction")
    parser.add_argument("--votes", type=int, help="classification passes per contract; >1 keeps only majority hits")
    parser.add_argument("--exclude-seed", type=int, action="append",
                        help="remove the sample this seed would draw from the pool first; repeat to chain several "
                             "(each seed draws from the pool left by the ones before it)")
    parser.add_argument("--exclude-n", type=int, default=30, help="size of the excluded sample (default 30)")
    parser.add_argument("--out", help="history file name for this run; refuses to overwrite an existing file")
    sys.exit(asyncio.run(main(parser.parse_args())))
