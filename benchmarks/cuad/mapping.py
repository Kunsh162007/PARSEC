"""Which of Fine Print's rules is expected to fire on which CUAD clause category.

CUAD labels 41 kinds of clause in 510 real commercial contracts. Fine Print's rules cover a subset of them,
and some CUAD categories (governing law, document name, parties, dates) are facts, not risks, so they are not
mapped. The mapping is deliberately generous: a rule counts as a detection if it fires on any listed category.
"""
from typing import Dict, List, Set

RULE_TO_CATEGORIES: Dict[str, List[str]] = {
    "leg-coc": ["Change Of Control"],
    "leg-assignment": ["Anti-Assignment"],
    "leg-tfc": ["Termination For Convenience"],
    "leg-restrictive": [
        "Exclusivity", "Non-Compete", "No-Solicit Of Employees", "No-Solicit Of Customers",
        "Rofr/Rofo/Rofn", "Most Favored Nation",
    ],
    "leg-autorenew": ["Renewal Term"],
    "leg-unlimited": ["Uncapped Liability"],
    "leg-penalty": ["Liquidated Damages", "Minimum Commitment"],
    "leg-ip": ["Joint Ip Ownership", "Ip Ownership Assignment"],
}

MAPPED_CATEGORIES: Set[str] = {c for cats in RULE_TO_CATEGORIES.values() for c in cats}
CATEGORY_TO_RULES: Dict[str, List[str]] = {
    c: [r for r, cats in RULE_TO_CATEGORIES.items() if c in cats] for c in sorted(MAPPED_CATEGORIES)
}
