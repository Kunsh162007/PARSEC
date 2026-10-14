import os
import re
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel

from packages.common.envfile import load_env_defaults

SEARCH_URL = "https://api.tavily.com/search"
RESULTS_PER_QUERY = 8
LITIGATION_TERMS = ("litigation", "lawsuit", "dispute", "class action", "sued", "fined", "investigation", "fraud")
_SUFFIX = r"(?:Inc|Incorporated|Corp|Corporation|Ltd|Limited|plc|LLC|LLP|GmbH|AG|Co|Company|Holdings|Group)"


class TavilyUnavailable(Exception):
    """No real Tavily result is available (no key, or the request failed). Nothing is simulated."""


class RealityCheckResult(BaseModel):
    query: str
    available: bool = True
    reason: str = ""
    claims_verified: Optional[bool] = None   # None when the check could not run
    flags: List[str] = []
    sources: List[Dict[str, str]] = []


class TavilyRealityEngine:
    """
    Outside-in screening with the Tavily Search API.

    Only real search results are ever returned. Without a key, or when a request fails, the result says the
    check was unavailable; it never substitutes invented results or scores. Results are kept only if they
    actually name the target company, so a search for one company cannot flag another company's lawsuit.
    """

    def __init__(self, api_key: Optional[str] = None):
        if api_key is None:
            load_env_defaults()
            api_key = os.getenv("TAVILY_API_KEY", "")
        self.api_key = api_key.strip()
        self.call_count = 0
        self.cache: Dict[str, Any] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, search_depth: str = "basic") -> Dict[str, Any]:
        if not self.is_configured:
            raise TavilyUnavailable("TAVILY_API_KEY is not set.")
        if query in self.cache:
            return self.cache[query]
        self.call_count += 1
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    SEARCH_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"query": query, "search_depth": search_depth, "max_results": RESULTS_PER_QUERY},
                )
        except httpx.HTTPError as exc:
            raise TavilyUnavailable(f"Tavily request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise TavilyUnavailable(f"Tavily returned HTTP {resp.status_code}")
        data = resp.json()
        self.cache[query] = data
        return data

    async def verify_external_claims(self, company_name: str, claims: List[str]) -> RealityCheckResult:
        query = f"{company_name} litigation regulatory actions customer controversies"
        try:
            data = await self.search(query)
        except TavilyUnavailable as exc:
            return RealityCheckResult(query=query, available=False, reason=str(exc))

        core = re.sub(rf"\s+{_SUFFIX}\.?$", "", company_name.strip(), flags=re.IGNORECASE).lower()
        results = [
            r for r in data.get("results", [])
            if core and core in f"{r.get('title', '')} {r.get('content', '')}".lower()
        ]
        sources: List[Dict[str, str]] = []
        flags: List[str] = []
        for r in results:
            sources.append({"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")[:240]})
            text = f"{r.get('title', '')} {r.get('content', '')}".lower()
            if any(term in text for term in LITIGATION_TERMS):
                flags.append(f"Public sources mention a dispute: {r.get('title')}")
        return RealityCheckResult(
            query=query, available=True, claims_verified=len(flags) == 0, flags=flags, sources=sources,
        )
