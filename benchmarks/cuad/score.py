"""Score findings against lawyer-labeled clause spans. Pure functions, no I/O.

Recall:    of the contracts where a lawyer labeled a clause of a category, in how many did a finding from a rule
           mapped to that category highlight (overlap) a labeled span?
Precision: of the places a rule fired, how many sit on a labeled span? Reported against the categories the rule is
           mapped to ("on_label") and against any of the 41 categories ("on_any_label"). Contracts can contain
           clauses the labelers did not mark, so precision here is a lower bound, not an exact figure.
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from benchmarks.cuad.mapping import CATEGORY_TO_RULES, MAPPED_CATEGORIES, RULE_TO_CATEGORIES

Span = Tuple[int, int]


def _overlaps(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _spans(finding: Dict[str, Any]) -> List[Span]:
    return [(s["start"], s["end"]) for s in finding.get("spans", []) if s.get("doc", 0) == 0]


def _hits_label(spans: Iterable[Span], labels: Iterable[Span]) -> bool:
    labels = list(labels)
    return any(_overlaps(s, l) for s in spans for l in labels)


def score_contract(labels: Dict[str, List[Span]], findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-contract detection by category and hit counts by rule."""
    categories: Dict[str, Dict[str, bool]] = {}
    for category, spans in labels.items():
        if category not in MAPPED_CATEGORIES:
            continue
        rules = CATEGORY_TO_RULES.get(category, [])
        detected = any(
            f.get("id") in rules and _hits_label(_spans(f), spans) for f in findings
        )
        categories[category] = {"labeled": bool(spans), "detected": bool(spans) and detected}

    all_label_spans = [s for spans in labels.values() for s in spans]
    rules: Dict[str, Dict[str, int]] = {}
    for f in findings:
        rule = f.get("id")
        if rule not in RULE_TO_CATEGORIES:
            continue
        mapped_spans = [s for c in RULE_TO_CATEGORIES[rule] for s in labels.get(c, [])]
        row = rules.setdefault(rule, {"hits": 0, "on_label": 0, "on_any_label": 0})
        for span in _spans(f):
            row["hits"] += 1
            row["on_label"] += int(_hits_label([span], mapped_spans))
            row["on_any_label"] += int(_hits_label([span], all_label_spans))
    return {"categories": categories, "rules": rules}


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 3) if den else None


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    cats: Dict[str, Dict[str, int]] = {}
    rules: Dict[str, Dict[str, int]] = {}
    for row in rows:
        for category, state in row["categories"].items():
            slot = cats.setdefault(category, {"contracts_with_clause": 0, "detected": 0})
            slot["contracts_with_clause"] += int(state["labeled"])
            slot["detected"] += int(state["detected"])
        for rule, counts in row["rules"].items():
            slot = rules.setdefault(rule, {"hits": 0, "on_label": 0, "on_any_label": 0, "contracts_fired": 0})
            for key in ("hits", "on_label", "on_any_label"):
                slot[key] += counts[key]
            slot["contracts_fired"] += 1
    return {
        "categories": {
            c: {**v, "recall": _rate(v["detected"], v["contracts_with_clause"])} for c, v in sorted(cats.items())
        },
        "rules": {
            r: {**v, "precision": _rate(v["on_label"], v["hits"]),
                "precision_any_label": _rate(v["on_any_label"], v["hits"])}
            for r, v in sorted(rules.items())
        },
    }
