"""Work out what a file still needs before a review of it can be called complete.

Nothing here is a fixed checklist. A reasoning model reads the names, openings and headings of the documents that
were supplied, decides what kind of engagement this looks like, and lists the documents or information a competent
reviewer would still ask for, in the terms practitioners use. When it has no standard name for something it must say
what the thing is instead. If no model answers, the result says so; there is no default list.

An item the model reports as supplied must carry a quote, and the quote is checked against the documents.

config/dd_vocabulary.yaml (standard request names from public lists) is deliberately NOT sent to the model: in a live
comparison it pulled the model towards a generic M&A list in place of the requests specific to the documents shown.
It is used only after the model has answered, as a lexicon to label each item with a workstream.
"""
from __future__ import annotations

import asyncio
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import yaml

from packages.gateway.llm import GatewayUnavailable
from packages.review.deep import locate_quote

TASK = "document_requirements_json"
MAX_DOCUMENTS = 12
OPENING_CHARS = 1500
MAX_HEADINGS = 60
MAX_ITEMS = 25
STATUSES = ("supplied", "missing", "unclear")
PRIORITIES = ("high", "medium", "low")
# The review UI groups items by these ids, so they must not change.
WORKSTREAMS = ("financial", "legal", "commercial", "tax", "people", "it_security", "esg_environmental",
               "real_estate", "regulatory", "ip")
OTHER_WORKSTREAM = "other"

PROMPT = """/no_think
You are a senior due diligence analyst. You are shown the names, openings and headings of the documents that were
supplied for a review. You do not see the full text.

1. Work out what kind of engagement or case this appears to be, in one sentence. If you cannot tell, say so.
2. List the documents or information a competent reviewer would still need before the file could be called
   complete. Use the terms practitioners in that field use. Do not pad the list: only items this engagement
   genuinely needs.
3. If an item has no standard name that you are sure of, set "name_known" to false and explain in
   "what_it_is" what the thing is and what it contains, so a non-specialist can ask for it.
4. For each item, say whether it appears among the supplied documents: "supplied", "missing" or "unclear".
   For "supplied", "evidence" must be a short quote copied exactly from the material shown. Otherwise "".
5. "why_needed" must tie the item to something visible in the material, or to the kind of engagement.
6. A section inside a supplied document counts as supplied: an annual report contains its financial statements,
   notes, MD&A and auditor's report if its headings show them. Use the heading as the evidence.
7. Be brief: at most 10 items, highest priority first, and keep "what_it_is" and "why_needed" to one short
   sentence each (under 25 words).

Reply with JSON only, no other text:
{"engagement": "...", "items": [{"name": "...", "name_known": true, "what_it_is": "...",
 "why_needed": "...", "status": "supplied|missing|unclear", "evidence": "...", "priority": "high|medium|low"}]}
"""

VOCABULARY_PATH = Path(__file__).resolve().parents[2] / "config" / "dd_vocabulary.yaml"

# Labelling an item with a workstream is done here, after the model has answered, so the model's choice of items is
# never steered by a list. First the reference vocabulary is used as a lexicon; then these rules, for common names it
# does not contain. Rules are tried in order, so the more specific ones come first. Choices worth knowing:
# SEC periodic filings (10-K, 10-Q, 8-K, proxy / DEF 14A) are "financial", the workstream that reads them; board,
# charter and insurance documents are "legal" as in the vocabulary; a licence of IP or content is "ip", while permits
# and operating licences are "regulatory"; "title" counts as real estate only as title insurance, commitment or report,
# since "chain of title" is an IP term.
_KEYWORD_RULES = [
    ("it_security", r"\bsoc ?[12]\b|penetration|pen[ -]?test|vulnerabilit|cyber|information security|it security"
                    r"|iso 27001|disaster recovery|\bbcp\b"),
    ("tax", r"\btax(es|ation)?\b|transfer pricing|\bnexus\b"),
    ("esg_environmental", r"\besg\b|environmental|sustainab|emission|hazardous|asbestos|phase (i|1|ii|2) (esa|environmental)"),
    ("legal", r"\bboard\b|minutes|charter|bylaws|by-laws|resolution|certificate of incorporation|articles of"
              r"|insurance|litigation|settlement|capitali[sz]ation table|cap table|subsidiar|organi[sz]ation(al)? chart"
              r"|corporate structure|good standing"),
    ("people", r"employee|employment|payroll|headcount|workforce|compensation|severance|pension|benefit plan"
               r"|\borg chart|\bhr\b"),
    ("real_estate", r"\bleases?\b|lease agreement|estoppel|rent roll|\bdeeds?\b|zoning|title (insurance|commitment|report|polic)"
                    r"|\brent\b|\bcam\b|common area|operating expense reconciliation"),
    ("regulatory", r"\bsec\b|securities and exchange|regulator|enforcement|consent decree|permit|sanction|export control"
                   r"|antitrust|merger control|fcpa|bribery|gdpr|franchise disclosure|\bfdd\b|operating licen[cs]e"),
    ("ip", r"patent|trademark|copyright|intellectual property|\bip\b|open source|licen[cs]e agreement|licen[cs]ing"
           r"|domain name|royalt"),
    ("financial", r"\b10-?k\b|\b10-?q\b|\b8-?k\b|proxy|def 14a|financial statement|audit|md&a|management.s discussion"
                  r"|ebitda|earnings|revenue|cash flow|\bdebt\b|credit (facility|agreement)|loan|invoice|payment"
                  r"|capex|capital expenditure|budget|projection|forecast|working capital|receivable|payable|restat"
                  r"|accounting|quarterly|internal control|\bsox\b|\bfinancial\b"
                  r"|\bbank\b|ledger|trial balance|reconciliation|operating expense"),
    ("commercial", r"customer|supplier|franchisee|distributor|reseller|pricing|\bsales\b|competitor|market"
                   r"|service level|\bsla"),
    ("legal", r"agreement|contract|amendment|side letter|waiver|non-disclosure|\bnda\b|consent|exhibit"
              r"|statement of work|scope of (work|services)"),
]
_RULES = [(stream, re.compile(pattern, re.IGNORECASE)) for stream, pattern in _KEYWORD_RULES]
_LEX_STOP = {"and", "the", "for", "with", "from", "list", "copy", "copies", "years", "year", "last", "any", "all",
             "other", "documents", "document", "records", "record", "report", "reports", "schedule", "agreements",
             "agreement", "plus", "open", "closed", "sample", "top"}


def _tokens(text: str) -> set:
    """Content words, lower-cased, with a plural 's' dropped so 'contracts' meets 'contract'."""
    words = re.findall(r"[a-z0-9]+", text.lower().replace("&", " and "))
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words
            if (len(w) >= 3 or w == "ip") and not w.isdigit() and w not in _LEX_STOP}


@lru_cache(maxsize=4)
def _lexicon(path: str) -> tuple:
    """(workstream, tokens) for each name and alternative name in the vocabulary; empty if the file is unusable."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ()
    streams = data.get("workstreams") if isinstance(data, dict) else None
    if not isinstance(streams, dict):
        return ()
    entries = []
    for stream in WORKSTREAMS:
        entry = streams.get(stream)
        for name in (entry.get("requested") or []) if isinstance(entry, dict) else []:
            # "EBITDA bridge / schedule of adjustments (add-back schedule)" gives three alternatives. The bracketed part
            # is taken whole, so "Freedom-to-operate (FTO) analysis" gives "Freedom-to-operate analysis" and "FTO",
            # never a stray "analysis".
            bracketed = re.findall(r"\(([^()]*)\)", str(name))
            outside = re.sub(r"\([^()]*\)", " ", str(name))
            for alternative in re.split(r"\s/\s", outside) + bracketed:
                tokens = frozenset(_tokens(alternative))
                if tokens:
                    entries.append((stream, tokens))
    return tuple(entries)


def _lexicon_match(text: str, path: str) -> tuple:
    """(workstream, share of the vocabulary name's words found, words found) for the best vocabulary name."""
    words = _tokens(text)
    best = ("", 0.0, 0)
    for stream, tokens in _lexicon(path):
        hits = len(tokens & words)
        score = hits / len(tokens)
        if (score, hits) > best[1:]:
            best = (stream, score, hits)
    return best


def workstream_of(name: str, what_it_is: str = "", vocabulary_path: Path | str | None = None) -> str:
    """The workstream an item belongs to, or "other". A complete match on a vocabulary name of two or more words
    wins; then the keyword rules; then a partial vocabulary match on at least two words. The description is tried only
    when the name says nothing."""
    path = str(vocabulary_path or VOCABULARY_PATH)
    for text in (name, what_it_is):
        if not text:
            continue
        stream, score, hits = _lexicon_match(text, path)
        if score == 1.0 and hits >= 2:
            return stream
        for rule_stream, pattern in _RULES:
            if pattern.search(text):
                return rule_stream
        if score >= 0.5 and hits >= 2:
            return stream
    return OTHER_WORKSTREAM


_HEADING = re.compile(r"^[A-Z0-9][A-Za-z0-9 ,&'’()./:-]{2,80}$")


_STRUCTURAL = re.compile(r"^(?:item|part|note|schedule|exhibit|section|article|annex|appendix)\b", re.IGNORECASE)
# Structural headings carry official titles that run long ("Item 13. Certain Relationships and Related Transactions,
# and Director Independence"), so they get a wider limit, and a contents-table cell break ("Item 7. | Management's")
# is read as a space.
_STRUCTURAL_HEADING = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,&'’()./:;–—-]{2,200}$")
_CELL_BREAK = re.compile(r"\s*\|\s*")
MAX_STRUCTURAL_WORDS = 30
_NAME_STOP = {"and", "the", "for", "with", "from", "list", "copy", "copies", "documents", "document", "statement",
              "statements", "report", "reports", "schedule", "agreement", "agreements", "any", "all", "relevant", "period",
              "item", "items", "part", "note", "notes", "section", "article", "annex", "appendix", "exhibit"}


def _as_heading(line: str) -> str:
    """The line as a heading, or "" if it does not look like one."""
    stripped = line.strip()
    if _STRUCTURAL.match(stripped):
        joined = _CELL_BREAK.sub(" ", stripped).strip()
        if _STRUCTURAL_HEADING.match(joined) and len(joined.split()) <= MAX_STRUCTURAL_WORDS and not joined.endswith("."):
            return joined
    if _HEADING.match(stripped) and len(stripped.split()) <= 12 and not stripped.endswith("."):
        return stripped
    return ""


def headings_of(text: str) -> List[str]:
    """Headings from the whole document, not just its start. Structural ones ("Item 7.", "Note 4") are always kept;
    the rest are spread evenly so a long document is represented end to end."""
    found: List[str] = []
    for line in text.splitlines():
        heading = _as_heading(line)
        if heading and heading not in found:
            found.append(heading)
    structural = [h for h in found if _STRUCTURAL.match(h)][:MAX_HEADINGS]
    rest = [h for h in found if not _STRUCTURAL.match(h)]
    room = MAX_HEADINGS - len(structural)
    if room > 0 and rest:
        if len(rest) <= room:
            structural += rest
        else:  # evenly spaced from the first to the last, so the end of a long document is represented too
            picks = {round(i * (len(rest) - 1) / (room - 1)) for i in range(room)} if room > 1 else {0}
            structural += [rest[i] for i in sorted(picks)]
    order = {h: i for i, h in enumerate(found)}
    return sorted(structural, key=order.__getitem__)


def section_match(name: str, documents: List[Dict[str, Any]]) -> str:
    """A heading in the supplied documents that names the same thing as a requested item, or "". Used to stop an
    item being called missing when it is a section of a document that was supplied."""
    words = [w for w in re.findall(r"[a-z]{4,}", name.lower()) if w not in _NAME_STOP]
    if not words:
        return ""
    needed = words[:2]
    for doc in documents[:MAX_DOCUMENTS]:
        for heading in headings_of(str(doc.get("text", ""))):
            low = heading.lower()
            if all(w in low for w in needed):
                return f"{doc.get('name', 'a supplied document')}: \"{heading}\""
    return ""


# --- Matching item names ------------------------------------------------------------------------------------------
# Two runs of the model rarely phrase the same request identically ("Audited financial statements (FY2023-2025)" and
# "Audited financial statements"), so items are compared on their content words. Two names match when
#   * the Jaccard overlap of their content words is at least NAME_JACCARD (0.6), computed on the full names and on the
#     names with bracketed parts removed, whichever is higher; or
#   * every content word of the shorter name (at least two words) appears in the longer one, and the extra words of the
#     longer name are not qualifiers that change which document is meant ("restated", "unredacted", "amended"...).
# 0.6 is chosen so that a single shared modifier is not enough: "Audited financial statements" and "Restated financial
# statements" share two of four words (0.5) and stay apart, while "Board resolutions authorizing the agreement" and
# "Board resolutions authorizing agreement" (1.0) or "Material contracts" and "Material customer contracts" (0.67)
# meet. The containment rule catches the common case of one run adding detail ("Tax returns" / "Federal and state tax
# returns") that Jaccard alone would split.
NAME_JACCARD = 0.6
_QUALIFIERS = {"restated", "restatement", "unredacted", "redacted", "amended", "amendment", "draft", "interim",
               "executed", "signed", "pro", "forma", "proforma", "updated", "revised", "prior", "historical"}
_FILING = re.compile(r"\b(10|8|20|40)\s*[-‐‑–]?\s*([kqf])\b", re.IGNORECASE)
_BRACKETED = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")


def _name_tokens(text: str) -> frozenset:
    """Content words of a name; "10-K" / "10‑K" / "10K" all become "10k" so filings are compared by form."""
    return frozenset(_tokens(_FILING.sub(lambda m: f" {m.group(1)}{m.group(2).lower()} ", text)))


def _jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def name_similarity(a: str, b: str) -> float:
    """1.0 for a qualifier-free containment, else the best Jaccard of the full and bracket-free names."""
    full_a, full_b = _name_tokens(a), _name_tokens(b)
    if (full_a & _QUALIFIERS) != (full_b & _QUALIFIERS):
        return 0.0  # "Restated financial statements" is a different document from "Financial statements"
    bare_a, bare_b = _name_tokens(_BRACKETED.sub(" ", a)), _name_tokens(_BRACKETED.sub(" ", b))
    best = max(_jaccard(full_a, full_b), _jaccard(bare_a, bare_b))
    for small, large in ((bare_a, bare_b), (bare_b, bare_a)):
        if len(small) >= 2 and small <= large and not ((large - small) & _QUALIFIERS):
            return 1.0
    return best


def names_match(a: str, b: str) -> bool:
    return name_similarity(a, b) >= NAME_JACCARD


# --- Is the requested item one of the supplied documents? ---------------------------------------------------------
# The model sometimes asks for a document that was supplied ("Endorsement Agreement" when the endorsement agreement is
# the file). An item is taken to be a supplied document only on strong evidence from the document's name and the
# title-like lines of its opening (short lines near the top, and runs of capitalised words):
#   * at least SUPPLIED_MIN_HITS of the item's content words appear there, covering at least SUPPLIED_COVERAGE of them;
#   * at least one of those words is distinctive (an entity name or a document-type word such as "endorsement",
#     "cease", "10k"), not one of the generic words below that any finance document's title may carry;
#   * the item carries no qualifier ("unredacted", "restated", "amended"...), which asks for a different version;
#   * title-like lines are short lines near the top that are not sentences, legends or section headings ("Item 8.
#     Financial Statements" is a section of the document, which section_match handles, not the document itself);
#   * any year in the item also appears in the title text.
SUPPLIED_MIN_HITS = 2
SUPPLIED_COVERAGE = 0.8
TITLE_LINES = 20
TITLE_LINE_WORDS = 12
_GENERIC = {"agreement", "contract", "document", "report", "statement", "financial", "form", "annual", "quarterly",
            "quarter", "copy", "schedule", "exhibit", "order", "company", "inc", "corp", "corporation", "llc", "ltd",
            "plc", "incorporated", "fiscal", "result", "information", "material", "public", "filing", "commission",
            "securitie", "exchange", "release", "press", "notice", "letter", "full", "complete", "related", "partie",
            "party"}  # words as _tokens leaves them: "securities" -> "securitie", "parties" -> "partie"
_CAPS_RUN = re.compile(r"(?:\b[A-Z][A-Z0-9&.,'’-]+\s+){1,}[A-Z][A-Z0-9&.,'’-]+")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_SEC_NAME = re.compile(r"securities\s+and\s+exchange\s+commission", re.IGNORECASE)


_EXHIBIT_NAME = re.compile(r"\bEX-?\d", re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _title_text(doc: Dict[str, Any]) -> str:
    """The document's name and the title-like parts of its opening. A file named as an exhibit to a filing
    ("..._8-K_EX-10.19_...") is not that filing, so the filing's form is dropped from such a name."""
    name = re.sub(r"\.[a-z0-9]{2,4}$", "", str(doc.get("name", "")), flags=re.IGNORECASE)
    if _EXHIBIT_NAME.search(name):
        name = _FILING.sub(" ", name)
    opening = str(doc.get("text", ""))[:OPENING_CHARS]
    lines = [ln.strip() for ln in opening.splitlines() if ln.strip()][:TITLE_LINES]
    # Runs of capitals catch a title in text that has lost its line breaks; a wide gap of spaces ends a run.
    runs = [run.strip() for chunk in _LAYOUT_GAP.split(opening) for run in _CAPS_RUN.findall(chunk)]
    titles = [t for t in lines + runs if len(t.split()) <= TITLE_LINE_WORDS and not _is_legend(t)
              and not _STRUCTURAL.match(t) and not (t.endswith(".") and len(t.split()) > 3)]
    return " ".join([re.sub(r"[_]+", " ", name)] + titles)


# A filing legend ("CONFIDENTIAL INFORMATION HAS BEEN OMITTED ... FILED SEPARATELY ... PURSUANT TO A CONFIDENTIAL
# TREATMENT APPLICATION") names other documents; it is not a title, so a line with these words is skipped.
_LEGEND_WORDS = {"pursuant", "filed", "omitted", "has", "have", "been", "separately", "excluded", "requested",
                 "indicated"}
_LAYOUT_GAP = re.compile(r"[ \t]{4,}")


def _is_legend(text: str) -> bool:
    return bool(set(re.findall(r"[a-z]+", text.lower())) & _LEGEND_WORDS)


def _title_tokens(title: str) -> frozenset:
    tokens = set(_name_tokens(title))
    if _SEC_NAME.search(title):
        tokens.add("sec")
    return frozenset(tokens)


# An exhibit, schedule or other attachment is a part of a document, often one left out of the copy supplied
# ("Exhibit A (Services Schedule)" of a services agreement); naming it is not naming the document.
_PART_OF = re.compile(r"\b(?:exhibits?|schedules?|annex(?:es)?|appendix|appendices|attachments?|addend(?:um|a)"
                      r"|side letters?|signature pages?)\b", re.IGNORECASE)


def _phrase(text: str) -> str:
    return " " + _NON_WORD.sub(" ", text.lower()).strip() + " "


def supplied_document(name: str, documents: List[Dict[str, Any]]) -> str:
    """The name of a supplied document the item clearly refers to, or "".
    Either the content-word rule above holds, or the whole name (brackets removed, at least two words, one of them
    distinctive) appears word for word in the title text: "Endorsement Agreement" in "ENDORSEMENT AGREEMENT", which the
    word rule misses because "agreement" is not a content word."""
    wanted = _name_tokens(name)
    if not wanted or wanted & _QUALIFIERS or _PART_OF.search(name):
        return ""
    years = set(_YEAR.findall(name))
    bare = _BRACKETED.sub(" ", name)
    phrase = _phrase(bare)
    phrase_ok = len(phrase.split()) >= 2 and bool(_name_tokens(bare) - _GENERIC)
    for doc in documents[:MAX_DOCUMENTS]:
        title = _title_text(doc)
        if not years <= set(_YEAR.findall(title)):
            continue
        hits = wanted & _title_tokens(title)
        by_words = (len(hits) >= SUPPLIED_MIN_HITS and len(hits) / len(wanted) >= SUPPLIED_COVERAGE
                    and bool(hits - _GENERIC))
        if by_words or (phrase_ok and phrase in _phrase(title)):
            return str(doc.get("name", "a supplied document"))
    return ""


def build_digest(documents: List[Dict[str, Any]]) -> str:
    """The opening and the headings of each document. Enough to recognise a document, cheap to send."""
    parts = []
    for index, doc in enumerate(documents[:MAX_DOCUMENTS], start=1):
        text = str(doc.get("text", ""))
        headings = headings_of(text)
        parts.append(
            f"### Document {index}: {doc.get('name', f'Document {index}')} ({len(text.split()):,} words)\n"
            f"Opening:\n{text[:OPENING_CHARS].strip()}\n"
            f"Headings: {'; '.join(headings) if headings else '(none found)'}"
        )
    return "\n\n".join(parts)


_FLAT_OBJECT = re.compile(r"\{[^{}]*\}")
_ENGAGEMENT = re.compile(r'"engagement"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _salvage(clean: str) -> Dict[str, Any] | None:
    """Keep the complete items of a reply that was cut off. Items are flat objects, so each can be read alone."""
    items = []
    for chunk in _FLAT_OBJECT.findall(clean):
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and "name" in obj:
            items.append(obj)
    if not items:
        return None
    engagement = _ENGAGEMENT.search(clean)
    return {"engagement": engagement.group(1) if engagement else "", "items": items}


def _extract_json(raw: str) -> Dict[str, Any] | None:
    clean = raw.strip()
    clean = re.sub(r"^```(?:json)?|```$", "", clean, flags=re.MULTILINE).strip()
    start, end = clean.find("{"), clean.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(clean[start:end + 1])
    except ValueError:
        return _salvage(clean)
    return data if isinstance(data, dict) else None


def _shown_text(documents: List[Dict[str, Any]]) -> str:
    return "\n".join(str(d.get("text", "")) for d in documents[:MAX_DOCUMENTS])


def clean_items(raw_items: Any, documents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep well-formed items. A 'supplied' claim without a verifiable quote is downgraded to 'unclear'."""
    if not isinstance(raw_items, list):
        return []
    corpus = _shown_text(documents)
    items = []
    for raw in raw_items[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        status = str(raw.get("status", "unclear")).strip().lower()
        status = status if status in STATUSES else "unclear"
        evidence = str(raw.get("evidence", "")).strip()
        note = ""
        if status == "supplied" and (not evidence or locate_quote(corpus, evidence) is None):
            status, evidence = "unclear", ""
            note = "The model said this was supplied but its quote could not be found in the documents."
        document = supplied_document(name, documents) if status != "supplied" else ""
        if document:
            status, evidence = "supplied", ""
            note = f"This names a supplied document: {document}."
        elif status == "missing":
            similar = section_match(name, documents)
            if similar:
                status, note = "unclear", f"A section with a similar heading exists in {similar}. Check whether it is what is needed."
        priority = str(raw.get("priority", "medium")).strip().lower()
        items.append({
            "name": name,
            "name_known": bool(raw.get("name_known", True)),
            "what_it_is": str(raw.get("what_it_is", "")).strip(),
            "why_needed": str(raw.get("why_needed", "")).strip(),
            "status": status,
            "evidence": evidence if status == "supplied" else "",
            "priority": priority if priority in PRIORITIES else "medium",
            "workstream": workstream_of(name, str(raw.get("what_it_is", ""))),
            "note": note,
        })
    return items


# --- Consensus over several runs (self-consistency, Wang et al. 2022, arXiv:2203.11171) ----------------------------
# One call gives a list that changes noticeably from run to run. With samples > 1 the model is asked several times
# independently and an item is kept only when a majority of the runs that answered list a matching item (more than
# half: 2 of 3, 2 of 2, 3 of 5). Each kept item reports its agreement ("agreement" of "of_runs").
# Merging a kept group of items:
#   * status: the most common one. A tie means the runs disagree, and that is reported as "unclear" ("to confirm").
#     "supplied" can only win with a member whose quote, or document match, was verified by clean_items;
#   * priority: the most common one; a tie takes the higher priority, so a disagreement never hides an item;
#   * name: the shortest (fewest words, then fewest characters) among the members with the winning status, with that
#     member's description, reason, evidence, note and workstream. Nothing is unioned across members.
# DEFAULT_SAMPLES is the number of calls determine_requirements makes when the caller does not say. It stays 1: on the
# 8 sample sets (benchmarks/requirements/stability.py, history/samples_*), run-to-run overlap was 0.26 for 3-sample
# consensus at the route's temperature 0.0 (the concurrent calls are strongly correlated) and 0.33 at temperature 0.7
# only because the lists shrank to 1-7 items. A single-call baseline of 0.25 was printed during that work, but one of
# its two files was overwritten; the single-call pairs still saved give 0.15-0.16 across prompt versions. Three times
# the cost bought no stability that could be shown.
DEFAULT_SAMPLES = 1
MAX_SAMPLES = 7
_PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}


def _shortest(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return min(items, key=lambda i: (len(i["name"].split()), len(i["name"])))


def _merge_group(members: List[Dict[str, Any]], runs: int) -> Dict[str, Any]:
    status_counts = {s: sum(1 for m in members if m["status"] == s) for s in STATUSES}
    top = max(status_counts.values())
    leaders = [s for s, n in status_counts.items() if n == top]
    status = leaders[0] if len(leaders) == 1 else "unclear"
    priority_counts = {p: sum(1 for m in members if m["priority"] == p) for p in PRIORITIES}
    top_p = max(priority_counts.values())
    priority = min((p for p, n in priority_counts.items() if n == top_p), key=_PRIORITY_RANK.__getitem__)
    with_status = [m for m in members if m["status"] == status]
    chosen = _shortest(with_status or members)
    note = chosen["note"]
    if not with_status:  # a tie resolved to "unclear" that no run gave: say why
        note = "The model runs disagreed on whether this was supplied. Check it."
    return {**chosen, "status": status, "priority": priority, "note": note,
            "evidence": chosen["evidence"] if status == "supplied" else "",
            "agreement": len(members), "of_runs": runs}


def merge_runs(runs: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Items that a majority of runs agree on, merged, highest priority and agreement first."""
    groups: List[List[tuple]] = []  # each member is (run index, item)
    for run_index, items in enumerate(runs):
        for item in items:
            best, best_score = None, 0.0
            for group in groups:
                if any(r == run_index for r, _ in group):
                    continue  # one item per run per group, so agreement counts runs, not repetitions
                score = max(name_similarity(item["name"], other["name"]) for _, other in group)
                if score >= NAME_JACCARD and score > best_score:
                    best, best_score = group, score
            if best is None:
                groups.append([(run_index, item)])
            else:
                best.append((run_index, item))
    needed = len(runs) // 2 + 1
    kept = [_merge_group([item for _, item in group], len(runs)) for group in groups if len(group) >= needed]
    ranked = sorted(enumerate(kept), key=lambda p: (_PRIORITY_RANK[p[1]["priority"]], -p[1]["agreement"], p[0]))
    return [item for _, item in ranked][:MAX_ITEMS]


async def _ask_once(gateway, messages: list) -> Dict[str, Any]:
    """One model call: {"data", "model", "cost"} on success, else {"error", "model", "cost"}."""
    try:
        resp = await gateway.complete(task=TASK, messages=messages, use_cache=False)
    except (GatewayUnavailable, KeyError) as exc:
        return {"error": f"No reasoning model answered: {exc}", "model": "", "cost": 0.0}
    cost = getattr(getattr(resp, "usage", None), "cost_usd", 0.0) or 0.0
    data = _extract_json(resp.content)
    if data is None:
        return {"error": "The model's reply could not be read, so no requirements list was produced.",
                "model": resp.model_used, "cost": cost}
    return {"data": data, "model": resp.model_used, "cost": cost}


async def determine_requirements(gateway, documents: List[Dict[str, Any]],
                                 samples: int = DEFAULT_SAMPLES) -> Dict[str, Any]:
    """What the file needs, as judged by the model. Never a substitute list when the model is unavailable.
    With samples > 1 the model is asked that many times concurrently and only the items a majority agree on are kept."""
    if not documents:
        return {"available": False, "reason": "No documents were supplied.", "engagement": "", "items": []}
    samples = max(1, min(int(samples), MAX_SAMPLES))
    messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": build_digest(documents)}]
    answers = await asyncio.gather(*(_ask_once(gateway, messages) for _ in range(samples)))
    cost = sum(a["cost"] for a in answers)
    good = [a for a in answers if "data" in a]
    if not good:
        failed = answers[0]
        out = {"available": False, "reason": failed["error"], "engagement": "", "items": []}
        if failed["model"]:
            out.update(model=failed["model"], cost_usd=cost)
        if samples > 1:
            out.update(samples=samples, samples_ok=0)
        return out
    runs = [clean_items(a["data"].get("items"), documents) for a in good]
    out = {
        "available": True,
        "reason": "",
        "engagement": str(good[0]["data"].get("engagement", "")).strip(),
        "items": runs[0] if samples == 1 else merge_runs(runs),
        "model": good[0]["model"],
        "cost_usd": cost,
    }
    if samples > 1:
        out.update(samples=samples, samples_ok=len(good))
        if len(good) < samples:
            out["reason"] = f"{len(good)} of {samples} model runs answered; agreement is counted over those."
    return out
