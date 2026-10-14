"""Draft the findings memo an analyst would send, from findings that were already verified.

The model writes prose, which is where invented facts creep in. So the memo is held to the evidence:
- every point must cite at least one evidence item ([F3], [N2]); a point citing nothing that exists is dropped;
- every number in a point, in digits or in whole words, must appear in the evidence it cites, and a magnitude
  written after it ("million", "%") must follow the same number there; otherwise the point is dropped.
  Evidence ids written in the text ([F3], N2) are removed first, so they neither count as figures nor excuse one.
What was dropped is counted and returned, so the reader knows the memo was edited and why.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from packages.gateway.llm import GatewayUnavailable

TASK = "findings_memo_json"
MAX_FINDINGS = 40
MAX_NEEDS = 20
QUOTE_CHARS = 300

PROMPT = """/no_think
You are a senior due diligence analyst writing a short memo for a colleague who has not read the documents.
You are given numbered evidence: findings (F1, F2, ...) with the exact words from the documents, and items still
needed (N1, N2, ...). Use only this evidence. Do not add facts, figures, names or dates that are not in it.

Write 3 to 5 sections, most important first. Suggested headings: "What matters most", "Contract terms to negotiate",
"What we could not check", "Requests to send". Each point is one or two sentences and must end with the ids it rests
on. Every number you write must appear in the evidence you cite.

Reply with JSON only:
{"sections": [{"heading": "...", "points": [{"text": "...", "cites": ["F1", "N2"]}]}]}
"""

def build_evidence(findings: List[Dict[str, Any]], needs: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Numbered evidence items. The text of each is what its citations are checked against."""
    evidence: Dict[str, Dict[str, str]] = {}
    for n, f in enumerate(findings[:MAX_FINDINGS], start=1):
        quote = str(f.get("quote", ""))[:QUOTE_CHARS]
        evidence[f"F{n}"] = {
            "kind": "finding",
            "text": f"{f.get('severity', '')} | {f.get('title', '')} | in {f.get('where', '')} | "
                    f"quote: \"{quote}\" | {f.get('detail', '')}".strip(),
        }
    for n, need in enumerate(needs[:MAX_NEEDS], start=1):
        evidence[f"N{n}"] = {
            "kind": "need",
            "text": f"still needed: {need.get('item', '')} | {need.get('why', '')}".strip(),
        }
    return evidence


_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
_WORD_RE = re.compile(r"\b(" + "|".join(_WORDS) + r")\b", re.IGNORECASE)

# A magnitude written right after a figure: "3 million", "40%", "12 percent". "3 million" and "3 billion" share the
# digit 3, so the digit alone proves nothing: the rule is that a figure's magnitude in the memo must also follow that
# same figure somewhere in the cited evidence. A figure written without a magnitude is checked on the digits only.
_MAGNITUDE = r"(?:\s*(%|percent|thousand|million|billion)(?![A-Za-z]))?"
_NUMBER_MAG = re.compile(r"(\d[\d,]*(?:\.\d+)?)" + _MAGNITUDE, re.IGNORECASE)
_WORD_MAG = re.compile(r"\b(" + "|".join(_WORDS) + r")\b" + _MAGNITUDE, re.IGNORECASE)


def _magnitude(raw: str) -> str:
    low = (raw or "").lower()
    return "%" if low == "percent" else low


def _numbers(text: str) -> List[Tuple[str, str]]:
    """(figure, magnitude) for each figure in digits, e.g. ("12.4", "million"); magnitude is "" when none."""
    return [(n.replace(",", ""), _magnitude(m)) for n, m in _NUMBER_MAG.findall(text)]


def _number_words(text: str) -> List[Tuple[str, str]]:
    """(word, magnitude) for each figure in words, matched as whole words only ("ten" is not in "content")."""
    # "one" is skipped: as in "one of the parties" it is usually not a figure.
    return [(w.lower(), _magnitude(m)) for w, m in _WORD_MAG.findall(text) if w.lower() != "one"]


def _strip_ids(text: str, evidence: Dict[str, Dict[str, str]]) -> str:
    """Remove inline evidence ids (F3, [N12], (F3, N2)) so their digits are not read as figures in the point."""
    return re.sub(r"\b[FN]\d+\b", lambda m: " " if m.group(0) in evidence else m.group(0), text)


def _figures_backed(text: str, backing: str) -> bool:
    """Every figure in text, in digits or words, appears in backing, with the same magnitude if one is written."""
    digits = _numbers(backing)
    words = _number_words(backing)
    known = {n for n, _ in digits} | {w for w, _ in words}
    with_magnitude = {pair for pair in digits + words if pair[1]}
    for figure, magnitude in _numbers(text):
        if figure not in known or (magnitude and (figure, magnitude) not in with_magnitude):
            return False
    for word, magnitude in _number_words(text):
        forms = {word, str(_WORDS[word])}  # "six" is backed by "six" or by "6"
        if not forms & known or (magnitude and not any((f, magnitude) in with_magnitude for f in forms)):
            return False
    return True


def _parse(raw: str) -> List[Dict[str, Any]]:
    clean = re.sub(r"```(?:json)?", "", raw or "")
    start, end = clean.find("{"), clean.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(clean[start:end + 1])
            if isinstance(data, dict) and isinstance(data.get("sections"), list):
                return [s for s in data["sections"] if isinstance(s, dict)]
        except ValueError:
            pass
    return []


def validate_memo(raw: str, evidence: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """Keep only points that cite real evidence and whose numbers appear in that evidence."""
    sections, uncited, unsupported = [], 0, 0
    for section in _parse(raw):
        heading = str(section.get("heading", "")).strip()
        kept = []
        for point in section.get("points", []) if isinstance(section.get("points"), list) else []:
            if not isinstance(point, dict):
                continue
            text = str(point.get("text", "")).strip()
            cites = [str(c).strip().strip("[]") for c in point.get("cites", []) if isinstance(c, (str, int))]
            cites = [c for c in cites if c in evidence]
            if not text or not cites:
                uncited += 1
                continue
            backing_text = " ".join(evidence[c]["text"] for c in cites)
            if not _figures_backed(_strip_ids(text, evidence), backing_text):
                unsupported += 1
                continue
            kept.append({"text": text, "cites": cites})
        if heading and kept:
            sections.append({"heading": heading, "points": kept})
    return {"sections": sections, "dropped": {"uncited": uncited, "unsupported_numbers": unsupported}}


def _render(evidence: Dict[str, Dict[str, str]], engagement: str) -> str:
    lines = [f"Engagement as read: {engagement}" if engagement else "Engagement: not stated", ""]
    lines += [f"[{key}] {item['text']}" for key, item in evidence.items()]
    return "\n".join(lines)


async def draft_memo(gateway, findings: List[Dict[str, Any]], needs: List[Dict[str, Any]], engagement: str = "") -> Dict[str, Any]:
    """The memo, or a stated reason why there is none. Never a template filled in without the model."""
    evidence = build_evidence(findings, needs)
    if not evidence:
        return {"available": False, "reason": "There are no findings or open requests to write about.", "sections": []}
    try:
        resp = await gateway.complete(
            task=TASK,
            messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": _render(evidence, engagement)}],
            use_cache=False,
        )
    except GatewayUnavailable as exc:
        return {"available": False, "reason": f"No model answered: {exc}", "sections": []}
    checked = validate_memo(resp.content, evidence)
    cost = getattr(getattr(resp, "usage", None), "cost_usd", 0.0)
    if not checked["sections"]:
        return {"available": False, "reason": "The model's memo could not be read, or none of its points could be traced to the evidence.",
                "sections": [], "dropped": checked["dropped"], "model": resp.model_used, "cost_usd": cost}
    return {"available": True, "reason": "", **checked, "evidence": evidence, "model": resp.model_used, "cost_usd": cost}
