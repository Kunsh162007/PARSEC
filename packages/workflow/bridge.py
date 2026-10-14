"""Read an Adjusted EBITDA reconciliation from a real document, and check it with arithmetic.

Real reconciliations come in every layout, so a pattern cannot read them. A model reads the table and returns, for the
most recent period, the starting line, each adjustment and the stated Adjusted EBITDA, copying each table row exactly.
Then plain code decides what to trust:
- a row must exist in the document, and its value must appear in that row;
- the starting figure plus the adjustments must reach the stated total within rounding (half a unit per line).
A reconciliation that does not add up is reported as such. That is a finding in itself, not an error to hide.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from packages.gateway.llm import GatewayUnavailable
from packages.review.deep import locate_quote

TASK = "ebitda_bridge_json"
EXCERPT_CHARS = 14000
CONTEXT_LINES = 45

PROMPT = """/no_think
You are reading an earnings release or a financial document. Find the reconciliation to Adjusted EBITDA (the table that
starts from net income, operating profit or a similar measure, adds or subtracts items, and ends at Adjusted EBITDA).

Use only the MOST RECENT period column (normally the first number column). Return:
- "period": that column's heading, e.g. "Three months ended September 30, 2025";
- "units": as stated, e.g. "in millions" or "in thousands", or "" if not stated;
- "start": the first line of the reconciliation;
- "adjustments": every line between the start and the Adjusted EBITDA total, in order, skipping subtotals;
- "total": the Adjusted EBITDA line.
For each line give "label", "value" (a number; a value shown in parentheses or as a deduction is negative) and "row":
the full table row copied exactly as it appears, so it can be found in the document.

Reply with JSON only:
{"period": "...", "units": "...", "start": {"label": "...", "value": 0, "row": "..."},
 "adjustments": [{"label": "...", "value": 0, "row": "..."}], "total": {"label": "...", "value": 0, "row": "..."}}
If there is no Adjusted EBITDA reconciliation, reply {"none": true}.
"""

_NUM = re.compile(r"\(?-?\$?\s?\d[\d,]*(?:\.\d+)?\)?")


def reconciliation_excerpt(text: str) -> str:
    """The table lines around every mention of Adjusted EBITDA, so the model reads the right part of a long document."""
    lines = text.splitlines()
    keep = set()
    for i, line in enumerate(lines):
        if re.search(r"adjusted\s+ebitda", line, re.I):
            keep.update(range(max(0, i - CONTEXT_LINES), min(len(lines), i + 3)))
    picked = [lines[i] for i in sorted(keep) if lines[i].strip()]
    excerpt = "\n".join(picked) if picked else text
    return excerpt[-EXCERPT_CHARS:] if len(excerpt) > EXCERPT_CHARS else excerpt


def _to_decimal(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


_DASHES = {"—", "–", "-", "— ", "−"}
_PERIOD_WORDS = re.compile(
    r"\b(?:years?|ended|ending|quarters?|months|weeks|fiscal|fy|periods?|as\s+of|q[1-4]|h[12]|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)\b", re.IGNORECASE)


def row_values(row: str) -> List[Decimal]:
    """The numbers in a table row's value cells. Parentheses mean negative; a dash means zero.

    A bare 4-digit 19xx/20xx is skipped as a year only where a year is expected: in a cell that also holds words
    (a date such as "September 30, 2025", or a row without cells), or in a period header row, whose label is empty
    or speaks of periods ("Year ended", "Three Months Ended"). Elsewhere, "Other items | 2023" is the value 2023."""
    parts = row.split("|")
    cells = parts[1:] if len(parts) > 1 else [row]
    label = parts[0] if len(parts) > 1 else ""
    header = len(parts) > 1 and (not label.strip() or bool(_PERIOD_WORDS.search(label)))
    out: List[Decimal] = []
    for cell in cells:
        c = cell.strip()
        if c in _DASHES:
            out.append(Decimal("0"))
            continue
        years_here = header or bool(re.search(r"[A-Za-z]", c))
        for m in _NUM.findall(c):
            raw = m.strip()
            negative = (raw.startswith("(") and raw.endswith(")")) or raw.startswith("-") or c.startswith("$(")
            digits = re.sub(r"[^\d.]", "", raw)
            # A year is a whole number: "20.0" is twenty, not a year.
            is_year = (len(digits) == 4 and digits.isdigit() and digits.startswith(("19", "20"))
                       and "," not in raw and "$" not in raw)
            if not digits or (is_year and years_here):
                continue
            d = _to_decimal(digits)
            if d is not None:
                out.append(-d if negative else d)
    return out


def _norm(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


_FOOTNOTE = re.compile(r"\s*(?:\(\d{1,2}\)|\[\d{1,2}\]|\*+)\s*$")


def _clean_label(label: str) -> str:
    """Drop trailing footnote markers such as "(2)", "[1]" or "*", which are not part of the line's name."""
    previous = None
    while previous != label:
        previous, label = label, _FOOTNOTE.sub("", label)
    return label.strip()


def _label_precedes(text: str, found, row: str, key: str) -> bool:
    """The copied row names the line, or the document line names it before the value, or the document line's own
    label is part of the model's label (a sub-line given with its heading: "Stock compensation expense: Included in
    cost of services"). Without this, a "row" that is only a number ("1,234,567") would verify against any line
    that happens to contain that number."""
    if not key:
        return bool(re.search(r"[A-Za-z]", row))
    line_start = text.rfind("\n", 0, found[0]) + 1
    if key in _norm(row) or key in _norm(text[line_start:found[1]]):
        return True
    line_end = text.find("\n", found[1])
    line = text[line_start:line_end if line_end != -1 else len(text)]
    own = _norm(_clean_label(line.split("|")[0])) if "|" in line else ""
    return len(own) >= 4 and own in key


def _find_row(text: str, row: str, label: str, value: Decimal):
    """Where the line is in the document: the exact row, the row without added currency signs, or a line with the
    same label that contains this exact value. In every case the number is traced to a real line that carries
    the line's label."""
    label = _clean_label(label)
    key = _norm(label)
    for candidate in (row, row.replace("$", "")):
        found = locate_quote(text, candidate) if candidate.strip() else None
        if found and (value in row_values(text[found[0]:found[1]])) and _label_precedes(text, found, candidate, key):
            return found
    if len(key) < 4:
        return None
    for m in re.finditer(r"(?m)^.*\|.*$", text):
        line = m.group(0)
        if _norm(line.split("|")[0]).startswith(key) and value in row_values(line):
            return m.start(), m.end()
    return _find_in_running_text(text, label, value)


_FIRST_VALUE = re.compile(r"\s*(?:\$\s?)?(\(?-?\d[\d,]*(?:\.\d+)?\)?|[—–-](?=\s|$))")


def _find_in_running_text(text: str, label: str, value: Decimal):
    """Some releases set the reconciliation as running text, not a table: "Debt amendment costs - 37 - 781 ...".
    Accept the line only if the label is followed directly by this value as its first figure (the latest period)."""
    words = [re.escape(w) for w in re.findall(r"[A-Za-z0-9]+", label)]
    if len(words) < 2:
        return None
    pattern = re.compile(r"[^A-Za-z0-9]+".join(words), re.IGNORECASE)
    for m in pattern.finditer(text):
        first = _FIRST_VALUE.match(text, m.end())
        if not first:
            continue
        token = first.group(1)
        parsed = Decimal("0") if token in ("—", "–", "-") else (row_values(f"x | {token}") or [None])[0]
        if parsed == value:
            return m.start(), first.end()
    return None


def _decimals(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _check_line(line: Any, text: str, role: str) -> Dict[str, Any]:
    if not isinstance(line, dict):
        return {"role": role, "ok": False, "why": "missing"}
    value = _to_decimal(line.get("value"))
    label = str(line.get("label", "")).strip()
    if value is None:
        return {"role": role, "label": label, "ok": False, "why": "no number"}
    row = str(line.get("row", "")).strip()
    found = _find_row(text, row, label, value)
    if found is None:
        return {"role": role, "label": label, "value": value, "ok": False, "why": "no line in the document has this label and value"}
    checked = {"role": role, "label": label, "value": value, "ok": True, "start": found[0], "end": found[1]}
    matched = text[found[0]:found[1]]
    if "|" in matched:
        # Every period column of the row, as printed. Used to see whether an "adjustment" recurs in each period.
        checked["periods"] = row_values(matched)
    return checked


def _parse(raw: str) -> Optional[Dict[str, Any]]:
    clean = re.sub(r"```(?:json)?", "", raw or "")
    s, e = clean.find("{"), clean.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        data = json.loads(clean[s:e + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def verify_bridge(raw: str, text: str) -> Dict[str, Any]:
    """Turn the model's reply into a checked bridge with a status the reader can rely on."""
    data = _parse(raw)
    if data is None:
        return {"status": "unavailable", "reason": "The model's reply could not be read."}
    if data.get("none"):
        return {"status": "incomplete", "reason": "No Adjusted EBITDA reconciliation was found in the document."}
    start = _check_line(data.get("start"), text, "start")
    total = _check_line(data.get("total"), text, "total")
    adjustments = [_check_line(a, text, "adjustment") for a in (data.get("adjustments") or []) if isinstance(a, dict)]
    lines = [start, *adjustments, total]
    unverified = [l for l in lines if not l["ok"]]
    base = {"period": str(data.get("period", "")).strip(), "units": str(data.get("units", "")).strip(),
            "start": start, "adjustments": adjustments, "total": total,
            "unverified": [{"role": l["role"], "label": l.get("label", ""), "why": l["why"]} for l in unverified]}
    if unverified:
        return {"status": "incomplete", "reason": f"{len(unverified)} line(s) could not be traced to the document.", **base}
    computed = start["value"] + sum((a["value"] for a in adjustments), Decimal("0"))
    places = max(_decimals(l["value"]) for l in lines)
    tolerance = Decimal("0.5") * Decimal(10) ** -places * len(lines)
    difference = total["value"] - computed
    status = "foots" if abs(difference) <= tolerance else "does_not_foot"
    return {"status": status, "reason": "", **base, "computed_total": computed, "difference": difference, "tolerance": tolerance}


async def extract_bridge(gateway, text: str) -> Dict[str, Any]:
    """The checked bridge, or a stated reason why there is none. Nothing is filled in without the model."""
    excerpt = reconciliation_excerpt(text)
    try:
        resp = await gateway.complete(
            task=TASK,
            messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": excerpt}],
            use_cache=False,
        )
    except GatewayUnavailable as exc:
        return {"status": "unavailable", "reason": f"No model answered: {exc}"}
    out = verify_bridge(resp.content, text)
    out["model"] = resp.model_used
    out["cost_usd"] = getattr(getattr(resp, "usage", None), "cost_usd", 0.0)
    return out


def recurs_in_every_period(line: Dict[str, Any]) -> bool:
    """True when the row shows a non-zero amount, of the same sign, in every period column (at least two)."""
    periods = line.get("periods") or []
    if len(periods) < 2 or any(v == 0 for v in periods):
        return False
    return all(v > 0 for v in periods) or all(v < 0 for v in periods)


_YEAR = re.compile(r"\b(?:FY\s?'?(20\d{2}|\d{2})|(20\d{2}))\b")  # FY23, FY'23, FY2023, FY 2023, 2023
_STOP = {"and", "the", "of", "for", "net", "costs", "cost", "expense", "expenses", "other", "related", "charges", "fees"}


def ledger_years(label: str, documents: Dict[str, str], skip: str = "") -> Dict[str, Any]:
    """In which years the documents' ledgers or accounts show a cost like this add-back. Plain text matching: a line
    must share the label's distinctive words and carry a year and an amount."""
    words = [w for w in re.findall(r"[a-z]{4,}", label.lower()) if w not in _STOP]
    if not words:
        return {"years": [], "lines": []}
    years, lines = set(), []
    for name, text in documents.items():
        if name == skip:
            continue
        for line in text.splitlines():
            low = line.lower()
            if not all(w in low for w in words[:2]) or not re.search(r"\d[\d,]{2,}", line):
                continue
            for m in _YEAR.finditer(line):
                fy = m.group(1)
                years.add((fy if len(fy) == 4 else f"20{fy}") if fy else m.group(2))
            if _YEAR.search(line):
                lines.append({"document": name, "line": line.strip()[:200]})
    return {"years": sorted(years), "lines": lines[:4]}
