"""Thin a document to its candidate clauses, then have a reasoning model classify each one with a checked citation.

Why: a model asked to "read the contract" sees only the first ~96,000 characters and misses clauses buried later;
regex rules see everything but miss clauses worded in ways nobody anticipated. Here a deliberately broad net picks out
every passage that could plausibly be a clause of interest, across the whole document. The model then sees only those
numbered passages, decides which ones really establish a clause of a given kind, and must quote the exact words.

Citation rule: a classification is kept only if its quote is found inside the passage it names. Anything else is
dropped and counted. Offsets therefore always point at the source text.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Tuple

from packages.gateway.llm import GatewayUnavailable
from packages.review.deep import locate_quote

TASK = "clause_verification_json"
MAX_PASSAGE_CHARS = 700
MIN_PASSAGE_CHARS = 25
BATCH_SIZE = 20  # smaller batches need less reasoning per call; 30 ran out of tokens on ~1 call in 5
MAX_CONCURRENCY = 4

# category id -> (label, definition in reviewer's terms, broad net). The nets are intentionally loose: they only
# decide what the model is shown, never what is reported.
CATEGORIES: Dict[str, Tuple[str, str, str]] = {
    "change_of_control": ("Change of control",
        "Rights or obligations change (termination right, consent, payment) if a party is acquired, merges, or its control changes.",
        r"change (?:of|in) (?:the )?control|merger|merge[sd]?\b|acqui(?:re|red|sition)|consolidat|controlling interest|"
        r"sale of (?:all|substantially)|beneficial own|voting (?:securities|stock|power)"),
    "anti_assignment": ("Anti-assignment",
        "A party may not assign or transfer the agreement, or its rights, without the other party's consent.",
        r"\bassign|\btransfer|\bdelegat"),
    "termination_for_convenience": ("Termination for convenience",
        "A party may end the agreement without cause, for example on written notice.",
        r"terminat|cancel"),
    "exclusivity": ("Exclusivity",
        "A party must deal exclusively with the other, may not deal with others, or holds exclusive rights.",
        r"exclusiv|\bsole\b|\bonly (?:supplier|provider|distributor|source)"),
    "non_compete": ("Non-compete",
        "A party is restricted from competing or from running or investing in a competing business.",
        r"compet"),
    "no_solicit_employees": ("No-solicit of employees",
        "A party may not solicit, hire or induce away the other party's employees.",
        r"solicit|\bhire|\bemploy|induce|entice|recruit"),
    "no_solicit_customers": ("No-solicit of customers",
        "A party may not solicit or take the other party's customers, clients or suppliers.",
        r"solicit|customer|client|divert|interfere"),
    "rofr": ("Right of first refusal, offer or negotiation",
        "A party must first offer, or give the other the first chance to buy, license or negotiate, before dealing with others.",
        r"first (?:refusal|offer|negotiat)|right of first|prior to (?:selling|offering|entering)|before (?:offering|selling)"),
    "most_favored_nation": ("Most favoured nation",
        "A party must receive prices or terms at least as favourable as any third party receives.",
        r"favou?r|no less|not less|lowest|best (?:price|terms|rate)|equal to or (?:better|lower)|most|comparable"),
    "renewal_term": ("Renewal term",
        "The agreement renews or may be extended, automatically or by option, and for how long.",
        r"renew|extend|extension|successive|automatic"),
    "uncapped_liability": ("Uncapped liability",
        "A party's liability is unlimited, or certain claims are carved out of the limitation of liability.",
        r"liabilit|\blimit|\bcap\b|consequential|indemnif"),
    "liquidated_damages": ("Liquidated damages",
        "A fixed sum or formula is payable on breach, delay or early termination.",
        r"liquidated|damages|penalt|forfeit|termination fee|break fee"),
    "minimum_commitment": ("Minimum commitment",
        "A party must buy, sell, pay or order at least a minimum quantity, value or volume.",
        r"minimum|at least|not less than|no less than|shall (?:purchase|order|buy)|quota|target|commit"),
    "ip_ownership_assignment": ("IP ownership or assignment",
        "Intellectual property created or delivered is owned by, or assigned to, a particular party.",
        r"intellectual property|\bIP\b|patent|copyright|invention|work product|deliverable|\bown(?:s|ed|ership)?\b|title to"),
    "joint_ip_ownership": ("Joint IP ownership",
        "Intellectual property is owned jointly by the parties.",
        r"joint(?:ly)? own|co-own|jointly develop|joint (?:invention|intellectual)"),
}

NETS = {cid: re.compile(net, re.IGNORECASE) for cid, (_, _, net) in CATEGORIES.items()}

# The labellers' own instructions: the "Details:" text of each CUAD v1 question (CUAD_v1.json in CUAD_v1.zip, The
# Atticus Project, CC BY 4.0; Hendrycks et al., arXiv 2103.06268). Copied verbatim except that non-breaking spaces are
# plain spaces and curly quotes are straight quotes.
# tests/unit/test_review_clauses.py checks these against the zip when it is present.
OFFICIAL_SOURCE = "CUAD v1 question text, 'Details:' field (data/cuad/CUAD_v1.zip: CUAD_v1/CUAD_v1.json)"
OFFICIAL_DEFINITIONS: Dict[str, str] = {
    "change_of_control": "Does one party have the right to terminate or is consent or notice required of the "
                         "counterparty if such party undergoes a change of control, such as a merger, stock sale, "
                         "transfer of all or substantially all of its assets or business, or assignment by operation "
                         "of law?",
    "anti_assignment": "Is consent or notice required of a party if the contract is assigned to a third party?",
    "termination_for_convenience": "Can a party terminate this contract without cause (solely by giving a notice and "
                                   "allowing a waiting period to expire)?",
    "exclusivity": "Is there an exclusive dealing commitment with the counterparty? This includes a commitment to "
                   "procure all \"requirements\" from one party of certain technology, goods, or services or a "
                   "prohibition on licensing or selling technology, goods or services to third parties, or a "
                   "prohibition on collaborating or working with other parties), whether during the contract or after "
                   "the contract ends (or both).",
    "non_compete": "Is there a restriction on the ability of a party to compete with the counterparty or operate in a "
                   "certain geography or business or technology sector?",
    "no_solicit_employees": "Is there a restriction on a party's soliciting or hiring employees and/or contractors "
                            "from the counterparty, whether during the contract or after the contract ends (or both)?",
    "no_solicit_customers": "Is a party restricted from contracting or soliciting customers or partners of the "
                            "counterparty, whether during the contract or after the contract ends (or both)?",
    "rofr": "Is there a clause granting one party a right of first refusal, right of first offer or right of first "
            "negotiation to purchase, license, market, or distribute equity interest, technology, assets, products or "
            "services?",
    "most_favored_nation": "Is there a clause that if a third party gets better terms on the licensing or sale of "
                           "technology/goods/services described in the contract, the buyer of such "
                           "technology/goods/services under the contract shall be entitled to those better terms?",
    "renewal_term": "What is the renewal term after the initial term expires? This includes automatic extensions and "
                    "unilateral extensions with prior notice.",
    "uncapped_liability": "Is a party's liability uncapped upon the breach of its obligation in the contract? This also "
                          "includes uncap liability for a particular type of breach such as IP infringement or breach "
                          "of confidentiality obligation.",
    "liquidated_damages": "Does the contract contain a clause that would award either party liquidated damages for "
                          "breach or a fee upon the termination of a contract (termination fee)?",
    "minimum_commitment": "Is there a minimum order size or minimum amount or units per-time period that one party "
                          "must buy from the counterparty under the contract?",
    "ip_ownership_assignment": "Does intellectual property created by one party become the property of the "
                               "counterparty, either per the terms of the contract or upon the occurrence of certain "
                               "events?",
    "joint_ip_ownership": "Is there any clause providing for joint or shared ownership of intellectual property "
                          "between the parties to the contract?",
}

PROMPT = """You are a contract reviewer. Below is a list of numbered passages taken from one agreement, and a list of
clause types with definitions. For each passage that itself establishes one of these clause types, return a hit.

Rules:
- Only tag a passage if its own words establish the clause. A heading alone, a cross-reference, or a definition of a
  term is not enough.
- A passage may have more than one clause type. Most passages have none.
- "quote" must be copied exactly from that passage: the shortest fragment (at least 6 words) that shows the clause.
- Use only the category ids listed.

Reply with JSON only: {"hits": [{"id": <passage number>, "category": "<category id>", "quote": "<exact words>"}]}
If nothing qualifies, reply {"hits": []}.

Clause types:
"""

_SENTENCE_END = re.compile(r"(?<=[.;:])\s+(?=[A-Z(\"“])|\n\s*\n|\n(?=\s*(?:\(?[a-z0-9]{1,3}[.)]|\d+\.\d*)\s)")


def segment(text: str) -> List[Dict[str, Any]]:
    """Sentence-like passages with their offsets. Long ones are cut into windows; tiny ones are skipped."""
    passages: List[Dict[str, Any]] = []
    pos = 0
    bounds = [m.start() for m in _SENTENCE_END.finditer(text)] + [len(text)]
    for end in bounds:
        raw = text[pos:end]
        lead = len(raw) - len(raw.lstrip())
        start = pos + lead
        body = raw.strip()
        pos = end
        if len(body) < MIN_PASSAGE_CHARS:
            continue
        for offset in range(0, len(body), MAX_PASSAGE_CHARS):
            piece = body[offset:offset + MAX_PASSAGE_CHARS]
            if len(piece) >= MIN_PASSAGE_CHARS:
                passages.append({"start": start + offset, "end": start + offset + len(piece), "text": piece})
    return passages


def select_candidates(passages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Passages caught by any category's broad net, with the categories that caught them."""
    out = []
    for p in passages:
        nets = [cid for cid, net in NETS.items() if net.search(p["text"])]
        if nets:
            out.append({**p, "nets": nets})
    return out


# Options measured on CUAD dev 25, seed 11 (benchmarks/cuad/history/06_*_dev.json). Adopted only together:
# near-miss + /no_think lifted same-category precision 50.2% -> 59.9% with model recall 79% -> 78%, either 86% -> 85%.
# Alone, near-miss cost recall (72%) and /no_think cost precision (46.3%). The Nano gate cost recall in every variant.
# Both off. Together they raised dev precision from 50% to 60% at unchanged recall, but on 30 fresh held-out
# contracts precision was 45.6%, below the 52% of the plain prompt's earlier test sample, so the gain was not
# confirmed (benchmarks/cuad/history/06_nearmiss_nothink_fresh_test.json). The notes were also written from the same
# dev errors they were scored on. Kept as options for further measurement.
USE_NO_THINK = False   # prefix "/no_think" to the system prompt (ContractEval, arXiv 2508.03080: thinking can lower F1)
USE_NEAR_MISS = False  # add a one-line "Not this:" near-miss note to the categories that draw false positives
USE_GATE = False       # ask Nano a strict yes/no per verified hit (gate_hits); not adopted
# Round 7, CUAD dev 25 seed 11 (history/07_*_dev.json), adoption bar +5 points precision over the same-round plain run
# (53.7%, model recall 83%, either 86%): official definitions 56.2% / 79% / 85%; voting 3 at temperature 0.7
# 52.0% / 79% / 84% (single sampled passes 46.5-49.3%). Neither passed, so both stay off.
USE_OFFICIAL_DEFINITIONS = False  # define each category with CUAD's own labelling instruction (OFFICIAL_DEFINITIONS)
USE_VOTING = False     # self-consistency (Wang et al., arXiv 2203.11171): classify VOTES times, keep majority hits
VOTES = 3              # passes per document when voting
MIN_VOTES = 2          # passes that must tag an overlapping span with the same category
VOTE_TASK = "clause_verification_vote_json"  # same model as TASK, sampled at a non-zero temperature

# Near-miss notes, written from the false-positive patterns seen on CUAD dev contracts only.
NOT_THIS: Dict[str, str] = {
    "ip_ownership_assignment": "each party keeping its own existing or background IP, ownership of data, trademarks, "
                               "materials or physical property, or a warranty that a party owns something. Tag only IP "
                               "created or delivered by one party that becomes the other party's property.",
    "exclusivity": "a party having sole responsibility, authority or control over a task, or owning its own marks.",
    "anti_assignment": "limits on sublicensing, or on transferring shares, licensed software or other property, "
                       "rather than the agreement or the rights under it.",
    "termination_for_convenience": "termination for breach, insolvency or other cause, ending only one service or a "
                                   "third party's arrangement, or notice not to renew at the end of a term.",
    "uncapped_liability": "an ordinary indemnity or a list of losses covered; it must say liability is unlimited or "
                          "exclude claims from a cap.",
    "liquidated_damages": "a refund of fees already paid, or a statement that a remedy is exclusive.",
    "minimum_commitment": "an obligation to train, staff, market or report; it must be a minimum amount to buy, sell, pay or order.",
}


def definition_of(cid: str, official: bool | None = None) -> str:
    """The category's definition: CUAD's official instruction or the hand-written one."""
    use_official = USE_OFFICIAL_DEFINITIONS if official is None else official
    return OFFICIAL_DEFINITIONS[cid] if use_official else CATEGORIES[cid][1]


def _category_block(near_miss: bool = False, official: bool = False) -> str:
    lines = []
    for cid, (label, _, _) in CATEGORIES.items():
        line = f"- {cid}: {label}. {definition_of(cid, official)}"
        if near_miss and cid in NOT_THIS:
            line += f" Not this: {NOT_THIS[cid]}"
        lines.append(line)
    return "\n".join(lines)


def build_system_prompt(no_think: bool = False, near_miss: bool = False, official: bool = False) -> str:
    """The classification instructions, with the optional reasoning switch, near-miss notes and official definitions."""
    return ("/no_think\n" if no_think else "") + PROMPT + _category_block(near_miss, official)


def _render_batch(batch: List[Dict[str, Any]]) -> str:
    return "\n\n".join(f"[{n}] {p['text']}" for n, p in enumerate(batch))


_FLAT = re.compile(r"\{[^{}]*\}")


def parse_hits(raw: str) -> List[Dict[str, Any]]:
    """Hits from the reply; tolerant of fences, prose around the JSON, and a reply cut off mid-list."""
    clean = re.sub(r"```(?:json)?", "", raw or "")
    start, end = clean.find("{"), clean.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(clean[start:end + 1])
            if isinstance(data, dict) and isinstance(data.get("hits"), list):
                return [h for h in data["hits"] if isinstance(h, dict)]
        except ValueError:
            pass
    hits = []
    for chunk in _FLAT.findall(clean):
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and "category" in obj:
            hits.append(obj)
    return hits


def verify_hits(raw_hits: List[Dict[str, Any]], batch: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Keep hits whose passage exists, whose category is known, and whose quote is inside that passage."""
    kept, dropped = [], 0
    for h in raw_hits:
        try:
            n = int(h.get("id"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        category = str(h.get("category", "")).strip()
        if not (0 <= n < len(batch)) or category not in CATEGORIES:
            dropped += 1
            continue
        passage = batch[n]
        located = locate_quote(passage["text"], str(h.get("quote", "")))
        if located is None:
            dropped += 1
            continue
        s, e = located
        kept.append({"category": category, "start": passage["start"] + s, "end": passage["start"] + e,
                     "quote": passage["text"][s:e]})
    return kept, dropped


def _overlaps(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return a["category"] == b["category"] and a["start"] < b["end"] and b["start"] < a["end"]


def vote_hits(passes: List[List[Dict[str, Any]]], min_votes: int = MIN_VOTES) -> List[Dict[str, Any]]:
    """Hits that at least min_votes passes agree on (same category, overlapping span), one per agreement.

    Passes are read in order, so a kept hit carries the span and quote of the first pass that has it.
    """
    kept: List[Dict[str, Any]] = []
    for hits in passes:
        for h in hits:
            if any(_overlaps(h, k) for k in kept):
                continue
            if sum(any(_overlaps(h, o) for o in other) for other in passes) >= min_votes:
                kept.append(h)
    return kept


async def _classify(gateway, batches: List[List[Dict[str, Any]]], system: str, task: str,
                    semaphore: asyncio.Semaphore, stats: Dict[str, Any], errors: List[str]) -> List[Dict[str, Any]]:
    """One classification pass over all batches; failed calls are counted in stats and contribute no hits."""

    async def run(batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        async with semaphore:
            try:
                resp = await gateway.complete(
                    task=task,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": _render_batch(batch)}],
                    use_cache=False,
                )
            except GatewayUnavailable as exc:
                stats["failed_calls"] += 1
                errors.append(str(exc)[:160])
                return []
        stats["tokens"] += getattr(resp.usage, "total_tokens", 0) or 0
        stats["cost_usd"] += getattr(resp.usage, "cost_usd", 0.0) or 0.0
        kept, dropped = verify_hits(parse_hits(resp.content), batch)
        stats["dropped"] += dropped
        return kept

    results = await asyncio.gather(*(run(b) for b in batches))
    return [h for group in results for h in group]


async def verify_clauses(gateway, text: str, batch_size: int = BATCH_SIZE, no_think: bool | None = None,
                         near_miss: bool | None = None, official: bool | None = None,
                         votes: int | None = None) -> Dict[str, Any]:
    """Classify the candidate passages of one document. Calls that fail are counted, never filled in.

    With votes > 1 the passes run concurrently on VOTE_TASK and only hits that MIN_VOTES passes agree on are kept.
    """
    passages = segment(text)
    candidates = select_candidates(passages)
    batches = [candidates[i:i + batch_size] for i in range(0, len(candidates), batch_size)]
    system = build_system_prompt(USE_NO_THINK if no_think is None else no_think,
                                 USE_NEAR_MISS if near_miss is None else near_miss,
                                 USE_OFFICIAL_DEFINITIONS if official is None else official)
    n_votes = (VOTES if USE_VOTING else 1) if votes is None else max(1, int(votes))
    task = VOTE_TASK if n_votes > 1 else TASK
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    stats: Dict[str, Any] = {"tokens": 0, "cost_usd": 0.0, "failed_calls": 0, "dropped": 0}
    errors: List[str] = []

    passes = await asyncio.gather(*(_classify(gateway, batches, system, task, semaphore, stats, errors)
                                    for _ in range(n_votes)))
    hits = list(passes[0]) if n_votes == 1 else vote_hits(list(passes), min(MIN_VOTES, n_votes))
    out = {
        "hits": hits,
        "passages": len(passages),
        "candidates": len(candidates),
        "calls": len(batches) * n_votes,
        "failed_calls": stats["failed_calls"],
        "errors": errors[:5],
        "dropped": stats["dropped"],
        "tokens": stats["tokens"],
        "cost_usd": round(stats["cost_usd"], 6),
    }
    if n_votes > 1:
        out["votes"] = n_votes
        out["pass_hits"] = [list(p) for p in passes]
    return out


# How much each clause type matters in a review, and what to ask about it.
SEVERITY = {
    "change_of_control": "high", "uncapped_liability": "high", "exclusivity": "high", "non_compete": "high",
    "ip_ownership_assignment": "high", "joint_ip_ownership": "high", "most_favored_nation": "high",
    "minimum_commitment": "high", "liquidated_damages": "medium", "anti_assignment": "medium",
    "termination_for_convenience": "medium", "rofr": "medium", "no_solicit_employees": "medium",
    "no_solicit_customers": "medium", "renewal_term": "low",
}
ASK = "Confirm how this clause applies to the transaction, whether it was negotiated, and what it would cost to waive."
MAX_DOCUMENTS = 10


async def clause_findings(gateway, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Verified clause hits for each document, in the same finding shape as the deep read."""
    texts = [str(d.get("text", "")) for d in documents[:MAX_DOCUMENTS]]
    results = await asyncio.gather(*(verify_clauses(gateway, t) for t in texts))
    findings: List[Dict[str, Any]] = []
    totals = {"passages": 0, "candidates": 0, "calls": 0, "failed_calls": 0, "dropped": 0, "cost_usd": 0.0}
    if USE_GATE:
        gated = await asyncio.gather(*(gate_hits(gateway, t, out["hits"]) for t, out in zip(texts, results)))
        totals.update({"gate_rejected": 0, "gate_unchecked": 0, "gate_failed_calls": 0})
        results = [{**out, "hits": g["hits"], "cost_usd": out["cost_usd"] + g["cost_usd"]} for out, g in zip(results, gated)]
        for g in gated:
            totals["gate_rejected"] += g["rejected"]
            totals["gate_unchecked"] += g["unchecked"]
            totals["gate_failed_calls"] += g["failed_calls"]
    for doc_index, out in enumerate(results):
        for key in ("passages", "candidates", "calls", "failed_calls", "dropped", "cost_usd"):
            totals[key] += out[key]
        for hit in out["hits"]:
            label, definition = CATEGORIES[hit["category"]][0], definition_of(hit["category"])
            findings.append({
                "id": f"clause-{hit['category']}-{len(findings) + 1}",
                "source": "ai",
                "lens": "legal",
                "severity": SEVERITY.get(hit["category"], "medium"),
                "title": label,
                "why": f"{definition} Found by the reasoning model in a pre-selected passage; its quote was checked against the text.",
                "ask": ASK,
                "spans": [{"doc": doc_index, "start": hit["start"], "end": hit["end"]}],
                "adjudicated": False,
            })
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return {"findings": findings, "usage": totals}


# ---- optional second opinion: Ultra checks Super's tags ----
CONFIRM_TASK = "clause_confirmation_json"
CONFIRM_BATCH = 15
CONFIRM_CONTEXT = 300

CONFIRM_PROMPT = """/no_think
You are a senior contract lawyer checking a junior reviewer's work. Each numbered item gives a clause type, its
definition, the words the junior quoted, and the surrounding text. Decide whether the text really establishes that
clause type. Reject an item if it is only a heading, a cross-reference, a definition of a term, or a different kind of
clause. Reply with JSON only: {"verdicts": [{"id": <item number>, "keep": true}, ...]}"""


def _confirm_item(n: int, hit: Dict[str, Any], text: str) -> str:
    label, definition = CATEGORIES[hit["category"]][0], definition_of(hit["category"])
    window = text[max(0, hit["start"] - CONFIRM_CONTEXT): hit["end"] + CONFIRM_CONTEXT].replace("\n", " ")
    return f"[{n}] Clause type: {label}. Definition: {definition}\nQuoted: \"{hit['quote']}\"\nText: ...{window}..."


def parse_verdicts(raw: str) -> Dict[int, bool]:
    out: Dict[int, bool] = {}
    for chunk in _FLAT.findall(re.sub(r"```(?:json)?", "", raw or "")):
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and "id" in obj and isinstance(obj.get("keep"), bool):
            try:
                out[int(obj["id"])] = obj["keep"]
            except (TypeError, ValueError):
                continue
    return out


async def confirm_hits(gateway, text: str, hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Ultra's verdict on each tag. A tag with no verdict (call failed, item missing) is kept and counted."""
    batches = [hits[i:i + CONFIRM_BATCH] for i in range(0, len(hits), CONFIRM_BATCH)]
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    stats = {"rejected": 0, "unconfirmed": 0, "failed_calls": 0, "cost_usd": 0.0}

    async def run(batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        user = "\n\n".join(_confirm_item(n, h, text) for n, h in enumerate(batch))
        async with semaphore:
            try:
                resp = await gateway.complete(
                    task=CONFIRM_TASK,
                    messages=[{"role": "system", "content": CONFIRM_PROMPT}, {"role": "user", "content": user}],
                    use_cache=False,
                )
            except GatewayUnavailable:
                stats["failed_calls"] += 1
                stats["unconfirmed"] += len(batch)
                return batch
        stats["cost_usd"] += getattr(resp.usage, "cost_usd", 0.0) or 0.0
        verdicts = parse_verdicts(resp.content)
        kept = []
        for n, h in enumerate(batch):
            if n not in verdicts:
                stats["unconfirmed"] += 1
                kept.append(h)
            elif verdicts[n]:
                kept.append(h)
            else:
                stats["rejected"] += 1
        return kept

    groups = await asyncio.gather(*(run(b) for b in batches))
    stats["cost_usd"] = round(stats["cost_usd"], 6)
    return {"hits": [h for g in groups for h in g], **stats}


# ---- optional cheap grounding gate: Nano answers one strict yes/no per verified tag ----
GATE_TASK = "clause_gate_json"
GATE_BATCH = 10

GATE_PROMPT = """/no_think
You check clause tags. Each numbered item gives a clause type with its definition, a quoted fragment, and the text
around it. For each item answer one question strictly: do the quoted words, read in that context, themselves satisfy
the definition? Answer false if the words are only a heading, a cross-reference, a defined term, a mention of the
topic, or a different kind of clause. Reply with JSON only: {"verdicts": [{"id": <item number>, "keep": true}, ...]}"""


async def gate_hits(gateway, text: str, hits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Nano's yes/no on each tag. A tag the gate could not check (call failed, item missing) is kept and counted."""
    batches = [hits[i:i + GATE_BATCH] for i in range(0, len(hits), GATE_BATCH)]
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    stats = {"rejected": 0, "unchecked": 0, "failed_calls": 0, "cost_usd": 0.0}

    async def run(batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        user = "\n\n".join(_confirm_item(n, h, text) for n, h in enumerate(batch))
        async with semaphore:
            try:
                resp = await gateway.complete(
                    task=GATE_TASK,
                    messages=[{"role": "system", "content": GATE_PROMPT}, {"role": "user", "content": user}],
                    use_cache=False,
                )
            except GatewayUnavailable:
                stats["failed_calls"] += 1
                stats["unchecked"] += len(batch)
                return [{**h, "gate": "unchecked"} for h in batch]
        stats["cost_usd"] += getattr(resp.usage, "cost_usd", 0.0) or 0.0
        verdicts = parse_verdicts(resp.content)
        kept = []
        for n, h in enumerate(batch):
            if n not in verdicts:
                stats["unchecked"] += 1
                kept.append({**h, "gate": "unchecked"})
            elif verdicts[n]:
                kept.append({**h, "gate": "yes"})
            else:
                stats["rejected"] += 1
        return kept

    groups = await asyncio.gather(*(run(b) for b in batches))
    stats["cost_usd"] = round(stats["cost_usd"], 6)
    return {"hits": [h for g in groups for h in g], **stats}
