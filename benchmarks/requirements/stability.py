"""How stable is "what this file still needs" from one run to the next? Compare two samples.json-format files.

    python -m benchmarks.requirements.stability history/samples_code_labels_run1.json samples.json

Paths are taken relative to benchmarks/requirements/ when they do not exist as given. Per document set it reports:

* overlap: items of run A and run B are paired one to one, best name similarity first, using the same fuzzy name
  matching as the consensus step (packages.review.requirements.name_similarity, threshold NAME_JACCARD). The overlap
  is pairs / (items in A + items in B - pairs), a Jaccard index over items. 1.0 means the same list twice.
* the number of items in each run;
* "outside": the share of items whose workstream is one that none of the set's documents belongs to. The documents'
  workstreams are labelled from their file names with the same labeller (workstream_of), and items labelled "other" are
  not counted; a contract also counts as "legal". This is a rough sign of a generic, one-item-per-workstream list. Its limits: a request outside the
  documents' workstreams is often legitimate (tax returns for a franchise, insurance for a licence); a file name can be
  labelled wrongly or not at all (then every item counts as outside); and the labeller itself is keyword based. Read
  it as a direction, never as a count of bad items.

There is no answer key, so higher overlap means more repeatable, not more correct.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from packages.review.requirements import NAME_JACCARD, OTHER_WORKSTREAM, name_similarity, workstream_of

HERE = Path(__file__).resolve().parent


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.exists() else HERE / path


def load(path: str) -> Dict[str, Dict[str, Any]]:
    data = json.loads(_resolve(path).read_text(encoding="utf-8"))
    return {row["name"]: row for row in data.get("sets", [])}


def pair_items(a: List[str], b: List[str]) -> List[Tuple[int, int, float]]:
    """One-to-one pairs (index in a, index in b, similarity), best similarity first, above the matching threshold."""
    candidates = sorted(((name_similarity(x, y), i, j) for i, x in enumerate(a) for j, y in enumerate(b)), reverse=True)
    used_a, used_b, pairs = set(), set(), []
    for score, i, j in candidates:
        if score < NAME_JACCARD:
            break
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            pairs.append((i, j, score))
    return pairs


def document_workstreams(documents: List[str]) -> set:
    """Workstreams of the supplied documents, from their file names. A contract ("...agreement") is also "legal", the
    workstream that reads every contract, whatever its subject."""
    streams = set()
    for name in documents:
        readable = re.sub(r"[_]+", " ", name)  # "UPS_10-K_2020" -> "UPS 10-K 2020", so word boundaries hold
        streams.add(workstream_of(readable))
        if re.search(r"agreement|contract", readable, re.IGNORECASE):
            streams.add("legal")
    return streams - {OTHER_WORKSTREAM}


def outside_share(items: List[Dict[str, Any]], documents: List[str]) -> float:
    doc_streams = document_workstreams(documents)
    if not items:
        return 0.0
    outside = [i for i in items if i.get("workstream", OTHER_WORKSTREAM) not in doc_streams | {OTHER_WORKSTREAM}]
    return len(outside) / len(items)


def set_stability(row_a: Dict[str, Any], row_b: Dict[str, Any]) -> Dict[str, Any]:
    names_a = [i["name"] for i in row_a.get("items", [])]
    names_b = [i["name"] for i in row_b.get("items", [])]
    pairs = pair_items(names_a, names_b)
    union = len(names_a) + len(names_b) - len(pairs)
    return {
        "items_a": len(names_a), "items_b": len(names_b), "paired": len(pairs),
        "overlap": len(pairs) / union if union else 1.0,
        "outside_a": outside_share(row_a.get("items", []), row_a.get("documents", [])),
        "outside_b": outside_share(row_b.get("items", []), row_b.get("documents", [])),
        "workstreams_a": len({i.get("workstream") for i in row_a.get("items", [])}),
        "workstreams_b": len({i.get("workstream") for i in row_b.get("items", [])}),
        "cost_a": row_a.get("cost_usd", 0.0), "cost_b": row_b.get("cost_usd", 0.0),
    }


def compare(path_a: str, path_b: str) -> List[Tuple[str, Dict[str, Any]]]:
    a, b = load(path_a), load(path_b)
    return [(name, set_stability(a[name], b[name])) for name in a if name in b]


def main(argv: List[str] | None = None) -> int:
    try:  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Compare two runs of benchmarks/requirements/samples.py.")
    parser.add_argument("run_a")
    parser.add_argument("run_b")
    parser.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = parser.parse_args(argv)
    rows = compare(args.run_a, args.run_b)
    if args.json:
        print(json.dumps([{"set": n, **r} for n, r in rows], indent=2))
        return 0
    print(f"{'set':46} {'items A/B':>9} {'paired':>6} {'overlap':>7} {'outside A/B':>11} {'cost A+B':>9}")
    for name, r in rows:
        print(f"{name[:46]:46} {r['items_a']:>4}/{r['items_b']:<4} {r['paired']:>6} {r['overlap']:>7.2f} "
              f"{r['outside_a']:>5.2f}/{r['outside_b']:<5.2f} {r['cost_a'] + r['cost_b']:>9.4f}")
    if rows:
        mean = sum(r["overlap"] for _, r in rows) / len(rows)
        cost = sum(r["cost_a"] + r["cost_b"] for _, r in rows)
        print(f"{'mean overlap':46} {'':9} {'':6} {mean:>7.2f} {'':11} {cost:>9.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
