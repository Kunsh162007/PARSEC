"""Outside-in check for Fine Print: do public sources link the companies named in a document to disputes?

This module is live-only. Unlike packages/tavily/client.py it never substitutes simulated results: with no
real Tavily key it reports "unavailable", and a failed search is reported as a failure, never as a finding.
"""
import asyncio
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from packages.common.envfile import load_env_defaults

SearchFn = Callable[[str], Awaitable[List[Dict[str, Any]]]]

TAVILY_URL = "https://api.tavily.com/search"
MAX_ENTITIES = 3
MAX_SOURCES_PER_FINDING = 3
RESULTS_PER_QUERY = 8   # five proved too thin: the same query returned different matters on different calls

_SUFFIX = r"(?:Ltd|Limited|Inc|Incorporated|Corp|Corporation|plc|PLC|LLC|LLP|GmbH|AG|S\.A\.|Co\.|Company|Holdings|Group)"
ENTITY_RE = re.compile(rf"\b((?:[A-Z][\w&'’\-]*\s+){{1,4}}{_SUFFIX})(?![\w])")
LEADING_NOISE = {"The", "This", "That", "And", "Between", "Agreement", "Section", "Schedule", "Party", "Parties"}

RISK_TERMS = (
    "lawsuit", "sued", "litigation", "class action", "fined", "penalty", "penalties", "fraud", "investigation",
    "investigated", "probe", "sanction", "recall", "bankrupt", "insolven", "settlement", "indicted",
    "data breach", "cease and desist", "violation", "regulator", "enforcement", "whistleblower",
)
# A term must start a word, so "fined" is not found in "refined" nor "sued" in "issued"; endings are allowed, so
# "sanctions" and "insolvency" still count.
RISK_PATTERNS = tuple((term, re.compile(rf"\b{re.escape(term)}\w*")) for term in RISK_TERMS)
QUERY_SUFFIX = "lawsuit OR fined OR investigation OR fraud OR recall OR sanctions OR bankruptcy"
MAX_WEB_QUERIES = 9   # per check: one query per (company, category), most-mentioned company first


@dataclass(frozen=True)
class WebTopic:
    """What to ask the internet about a company for one due diligence category."""
    lens: str
    label: str                  # completes "Public sources link {name} to ..."
    query: str                  # appended to the quoted company name
    terms: Tuple[str, ...]      # a result counts only if it mentions one of these (word start, endings allowed)
    ask: str                    # the question for the other side; {name} is filled in

    @property
    def patterns(self) -> Tuple[Tuple[str, "re.Pattern[str]"], ...]:
        return tuple((t, re.compile(rf"\b{re.escape(t)}\w*")) for t in self.terms)


GENERAL_TOPIC = WebTopic(
    "compliance", "a dispute or enforcement matter", QUERY_SUFFIX, RISK_TERMS,
    "Ask {name} to explain each matter listed and to provide the underlying documents and current status.",
)

CATEGORY_TOPICS: Dict[str, WebTopic] = {t.lens: t for t in (
    WebTopic("financial", "accounting or financial-reporting problems",
             'restatement OR "accounting irregularities" OR "auditor resigned" OR "going concern" OR "SEC charges"',
             ("restat", "accounting irregular", "auditor", "going concern", "material weakness", "sec charge",
              "accounting fraud", "write-down", "impairment", "earnings miss", "profit warning"),
             "Ask {name} for the restated or corrected figures, the auditor's letters on each matter and how it "
             "affects the numbers you were given."),
    WebTopic("legal", "litigation or contract disputes",
             'lawsuit OR litigation OR "breach of contract" OR settlement OR "court ruling"',
             ("lawsuit", "sued", "litigation", "class action", "breach of contract", "settlement", "court",
              "injunction", "arbitration", "patent infringement"),
             "Ask {name} for a schedule of open and recent litigation, the amounts claimed, the provisions booked "
             "and counsel's view of each case."),
    WebTopic("compliance", "regulatory action",
             'fined OR sanctions OR regulator OR investigation OR penalty OR bribery',
             ("fined", "penalt", "sanction", "regulator", "investigation", "probe", "bribery", "corruption",
              "enforcement", "consent order", "money laundering", "violation"),
             "Ask {name} for all correspondence with regulators on each matter, the remediation done and any "
             "undertakings still in force."),
    WebTopic("tech", "security incidents or outages",
             '"data breach" OR cyberattack OR ransomware OR outage OR "security vulnerability"',
             ("data breach", "breach", "cyberattack", "cyber attack", "ransomware", "hack", "outage",
              "vulnerabilit", "leak", "exposed data"),
             "Ask {name} for the incident report on each event, what data was affected, the regulator and "
             "customer notifications made and the controls changed since."),
    WebTopic("people", "workforce or management problems",
             'layoffs OR strike OR "discrimination lawsuit" OR "executive departure" OR "labor dispute"',
             ("layoff", "redundanc", "strike", "discrimination", "harassment", "wrongful dismissal",
              "labor dispute", "labour dispute", "union", "resign", "executive departure", "wage theft"),
             "Ask {name} about each matter: the people and claims involved, settlements paid, key-person "
             "retention and any open employment tribunal or labour cases."),
    WebTopic("property", "environmental or site problems",
             'contamination OR "environmental violation" OR EPA OR spill OR "zoning dispute"',
             ("contaminat", "environmental violation", "pollut", "spill", "epa", "emission", "toxic",
              "zoning", "permit", "site closure", "remediation"),
             "Ask {name} for the environmental reports, permits and remediation costs for each site named, and "
             "whether any authority has open proceedings."),
    WebTopic("tax", "tax disputes",
             '"tax dispute" OR "tax evasion" OR "back taxes" OR "tax audit" OR "transfer pricing"',
             ("tax dispute", "tax evasion", "back tax", "tax audit", "transfer pricing", "tax fraud",
              "irs", "hmrc", "tax authorit", "tax assessment", "unpaid tax"),
             "Ask {name} for the tax authority correspondence on each matter, the amounts assessed or at risk, "
             "and the tax provisions booked against them."),
    WebTopic("commercial", "lost customers or market problems",
             '"lost contract" OR "customer loss" OR "revenue decline" OR "market share" OR churn',
             ("lost contract", "lost customer", "customer loss", "terminated contract", "contract terminated",
              "revenue decline", "sales fell", "market share", "churn", "downgrade", "profit warning"),
             "Ask {name} which customers and contracts were affected, the revenue at stake and how the "
             "forecasts you were given allow for it."),
    WebTopic("supply", "supply-chain or ESG problems",
             '"supply chain disruption" OR "forced labor" OR recall OR "supplier" OR greenwashing',
             ("supply chain", "shortage", "forced labo", "child labo", "recall", "supplier", "greenwash",
              "esg", "human rights", "disruption", "boycott"),
             "Ask {name} which suppliers and products were involved, the audits done since, and any "
             "single-source dependency that remains."),
)}


def extract_entities(documents: List[Dict[str, str]]) -> List[str]:
    """Company-like names ranked by how often they appear across the documents."""
    counts: Counter = Counter()
    for doc in documents:
        for match in ENTITY_RE.finditer(str(doc.get("text", ""))):
            words = match.group(1).split()
            while words and words[0] in LEADING_NOISE:
                words.pop(0)
            if len(words) >= 2:
                counts[" ".join(words)] += 1
    return [name for name, _ in counts.most_common()]


def _http_search(api_key: str) -> SearchFn:
    async def search(query: str) -> List[Dict[str, Any]]:
        import httpx  # local import keeps the module importable in minimal environments

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                TAVILY_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"query": query, "search_depth": "basic", "max_results": RESULTS_PER_QUERY, "include_answer": False},
            )
            resp.raise_for_status()
            return list(resp.json().get("results", []))

    return search


def _is_http_url(url: str) -> bool:
    return bool(re.match(r"^https?://", str(url or ""), re.IGNORECASE))


def _first_mention(documents: List[Dict[str, str]], name: str) -> Optional[Dict[str, int]]:
    """The name's first span in the documents. The name was rebuilt with single spaces, so the document may have
    several spaces or a line break between its words: any run of whitespace is accepted there."""
    pattern = re.compile(r"\s+".join(re.escape(w) for w in name.split()))
    for index, doc in enumerate(documents):
        match = pattern.search(str(doc.get("text", "")))
        if match:
            return {"doc": index, "start": match.start(), "end": match.end()}
    return None


def _about_entity(result: Dict[str, Any], name: str, patterns=RISK_PATTERNS) -> Optional[str]:
    """The risk term if this result is about the named company and mentions a matter of this kind, else None."""
    text = f"{result.get('title', '')} {result.get('content', '')}".lower()
    core = re.sub(rf"\s+{_SUFFIX}$", "", name, flags=re.IGNORECASE).lower()
    if core not in text:
        return None
    return next((term for term, pattern in patterns if pattern.search(text)), None)


def _topics_for(categories: Optional[Sequence[str]]) -> List[WebTopic]:
    """One search topic per chosen category; the general dispute search when none applies (or integrity only)."""
    chosen = [CATEGORY_TOPICS[c] for c in dict.fromkeys(categories or []) if c in CATEGORY_TOPICS]
    return chosen or [GENERAL_TOPIC]


async def web_check(
    documents: List[Dict[str, str]],
    search: Optional[SearchFn] = None,
    api_key: Optional[str] = None,
    max_entities: int = MAX_ENTITIES,
    retries: int = 1,
    retry_delay: float = 0.8,
    categories: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Look up the companies named in the documents. With categories, each company is searched once per category
    with that category's own query and relevance terms, and each hit is asked about in that category's terms."""
    if api_key is None:
        load_env_defaults()  # CLI and benchmark runs do not go through the API's startup
    key = os.getenv("TAVILY_API_KEY", "") if api_key is None else api_key
    if search is None:
        if not key or key.startswith("mock"):
            return {"available": False, "reason": "No Tavily API key is configured on the server.",
                    "checked": [], "lookups": [], "findings": []}
        search = _http_search(key)

    entities = extract_entities(documents)[:max_entities]
    if not entities:
        return {"available": True, "reason": "", "checked": [], "lookups": [], "findings": []}

    topics = _topics_for(categories)
    by_category = topics[0] is not GENERAL_TOPIC
    wanted = [(name, topic) for name in entities for topic in topics]
    plan = wanted[:MAX_WEB_QUERIES]

    async def lookup(name: str, topic: WebTopic):
        """(results, error). One retry by default: a single throttled call should not read as 'no matters'."""
        error = ""
        for attempt in range(retries + 1):
            try:
                return list(await search(f'"{name}" {topic.query}')), ""
            except Exception as exc:  # the caller reports it; it is never turned into a finding
                error = f"{type(exc).__name__}: {exc}"[:160]
                if attempt < retries and retry_delay:
                    await asyncio.sleep(retry_delay)
        return None, error

    outcomes = await asyncio.gather(*(lookup(name, topic) for name, topic in plan))

    findings: List[Dict[str, Any]] = []
    lookups: List[Dict[str, Any]] = []
    failures = 0
    for (name, topic), (results, error) in zip(plan, outcomes):
        tag = {"category": topic.lens} if by_category else {}
        if results is None:
            failures += 1
            lookups.append({"entity": name, "results": 0, "relevant": 0, "error": error, **tag})
            continue
        sources, terms = _relevant_sources(results, name, topic)
        lookups.append({"entity": name, "results": len(results), "relevant": len(sources), "error": "", **tag})
        span = _first_mention(documents, name)
        if sources and span:
            findings.append(_web_finding(len(findings) + 1, name, topic, terms[0], span, sources))

    if failures == len(plan):
        return {"available": False, "reason": "The Tavily search failed, so no web check was made.",
                "checked": entities, "lookups": lookups, "findings": []}
    notes = []
    if failures:
        notes.append(f"{failures} of {len(plan)} company lookups failed and were not checked.")
    if len(wanted) > len(plan):
        skipped = wanted[len(plan):]
        notes.append(f"{len(skipped)} company and category searches were not searched (limit of {MAX_WEB_QUERIES} "
                     f"per check): " + ", ".join(f"{n} ({t.lens})" for n, t in skipped) + ".")
    return {"available": True, "reason": " ".join(notes), "checked": entities, "lookups": lookups,
            "findings": findings}


def _relevant_sources(results: List[Dict[str, Any]], name: str, topic: WebTopic) -> Tuple[List[Dict[str, str]], List[str]]:
    """The results about this company that mention a matter of this topic, with the term that matched each."""
    patterns = RISK_PATTERNS if topic is GENERAL_TOPIC else topic.patterns
    sources, terms = [], []
    for result in results:
        term = _about_entity(result, name, patterns)
        if term and _is_http_url(result.get("url", "")):
            sources.append({
                "title": str(result.get("title", ""))[:160],
                "url": str(result["url"]),
                "snippet": str(result.get("content", ""))[:240],
            })
            terms.append(term)
    return sources, terms


def _web_finding(n: int, name: str, topic: WebTopic, term: str, span: Dict[str, int],
                 sources: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "id": f"web-{n}",
        "source": "web",
        "lens": topic.lens,
        "severity": "medium",
        "title": f"Public sources link {name} to {topic.label}",
        "why": f"Search results mention {name} together with \"{term}\". Results can concern a "
               "different company with a similar name or be out of date, so confirm each source before relying on it.",
        "ask": topic.ask.format(name=name),
        "spans": [span],
        "sources": sources[:MAX_SOURCES_PER_FINDING],
    }
