"""Draft the findings memo an analyst would send, from findings that were already verified.

The model writes prose, which is where invented facts creep in. So the memo is held to the evidence:
- every point must cite at least one evidence item ([F3], [N2]); a point citing nothing that exists is dropped;
- every number in a point, in digits or in words, must appear in the evidence it cites; otherwise it is dropped.
What was dropped is counted and returned, so the reader knows the memo was edited and why.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

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

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


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


def _numbers(text: str) -> List[str]:
    return [m.replace(",", "") for m in _NUMBER.findall(text)]


def _number_words(text: str) -> List[str]:
    # "one" is skipped: as in "one of the parties" it is usually not a figure.
    return [w.lower() for w in _WORD_RE.findall(text) if w.lower() != "one"]


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
            backing_numbers = set(_numbers(" ".join(evidence[c]["text"] for c in cites)))
            cite_numbers = {c[1:] for c in cites}  # "F3" -> "3", so an id written in the text is not a claim
            backing_text = " ".join(evidence[c]["text"] for c in cites).lower()
            words_ok = all(w in backing_text or str(_WORDS[w]) in backing_numbers for w in _number_words(text))
            if not words_ok or any(n not in backing_numbers and n not in cite_numbers for n in _numbers(text)):
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
