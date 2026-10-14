"""Outside-in check for Fine Print: do public sources link the companies named in a document to disputes?

This module is live-only. Unlike packages/tavily/client.py it never substitutes simulated results: with no
real Tavily key it reports "unavailable", and a failed search is reported as a failure, never as a finding.
"""
import asyncio
import os
import re
from collections import Counter
from typing import Any, Awaitable, Callable, Dict, List, Optional

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


def _about_entity(result: Dict[str, Any], name: str) -> Optional[str]:
    """The risk term if this result is about the named company and mentions a dispute, else None."""
    text = f"{result.get('title', '')} {result.get('content', '')}".lower()
    core = re.sub(rf"\s+{_SUFFIX}$", "", name, flags=re.IGNORECASE).lower()
    if core not in text:
        return None
    return next((term for term, pattern in RISK_PATTERNS if pattern.search(text)), None)


async def web_check(
    documents: List[Dict[str, str]],
    search: Optional[SearchFn] = None,
    api_key: Optional[str] = None,
    max_entities: int = MAX_ENTITIES,
    retries: int = 1,
    retry_delay: float = 0.8,
) -> Dict[str, Any]:
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

    async def lookup(name: str):
        """(results, error). One retry by default: a single throttled call should not read as 'no matters'."""
        error = ""
        for attempt in range(retries + 1):
            try:
                return list(await search(f'"{name}" {QUERY_SUFFIX}')), ""
            except Exception as exc:  # the caller reports it; it is never turned into a finding
                error = f"{type(exc).__name__}: {exc}"[:160]
                if attempt < retries and retry_delay:
                    await asyncio.sleep(retry_delay)
        return None, error

    outcomes = await asyncio.gather(*(lookup(name) for name in entities))

    findings: List[Dict[str, Any]] = []
    lookups: List[Dict[str, Any]] = []
    failures = 0
    for name, (results, error) in zip(entities, outcomes):
        if results is None:
            failures += 1
            lookups.append({"entity": name, "results": 0, "relevant": 0, "error": error})
            continue
        sources, terms = [], []
        for result in results:
            term = _about_entity(result, name)
            if term and _is_http_url(result.get("url", "")):
                sources.append({
                    "title": str(result.get("title", ""))[:160],
                    "url": str(result["url"]),
                    "snippet": str(result.get("content", ""))[:240],
                })
                terms.append(term)
        lookups.append({"entity": name, "results": len(results), "relevant": len(sources), "error": ""})
        span = _first_mention(documents, name)
        if sources and span:
            findings.append({
                "id": f"web-{len(findings) + 1}",
                "source": "web",
                "lens": "compliance",
                "severity": "medium",
                "title": f"Public sources link {name} to a dispute or enforcement matter",
                "why": f"Search results mention {name} together with \"{terms[0]}\". Results can concern a "
                       "different company with a similar name or be out of date, so confirm each source before relying on it.",
                "ask": f"Ask {name} to explain each matter listed and to provide the underlying documents and current status.",
                "spans": [span],
                "sources": sources[:MAX_SOURCES_PER_FINDING],
            })

    if failures == len(entities):
        return {"available": False, "reason": "The Tavily search failed, so no web check was made.",
                "checked": entities, "lookups": lookups, "findings": []}
    reason = f"{failures} of {len(entities)} company lookups failed and were not checked." if failures else ""
    return {"available": True, "reason": reason, "checked": entities, "lookups": lookups, "findings": findings}
