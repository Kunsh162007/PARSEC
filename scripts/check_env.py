"""Report which API keys are configured and, with --live, whether each one actually works.

    python scripts/check_env.py          # presence only, no network, no cost
    python scripts/check_env.py --live   # one tiny request per service (a fraction of a cent)

Never prints a key. Exit code is 0 only if every key that is set also works (with --live).
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.common.envfile import load_env_defaults  # noqa: E402
from packages.gateway.llm import GatewayUnavailable, LLMGateway  # noqa: E402
from packages.tavily.client import TavilyRealityEngine, TavilyUnavailable  # noqa: E402

SERVICES = (
    ("NEBIUS_API_KEY", "Nebius Token Factory", "AI deep read, clause check, what the file still needs, memo, PARSEC reconciliation reading and committee questions", "https://studio.nebius.com"),
    ("TAVILY_API_KEY", "Tavily Search", "public-source check on named companies", "https://app.tavily.com"),
)


async def check_nebius() -> str:
    gateway = LLMGateway()
    reply = await gateway.complete(
        task="document_triage_json", use_cache=False,
        messages=[{"role": "user", "content": 'Reply with exactly: {"flag": false}'}],
    )
    return f"answered by {reply.model_used} ({reply.usage.total_tokens} tokens)"


async def check_tavily() -> str:
    result = await TavilyRealityEngine().search("Tavily search API")
    return f"returned {len(result.get('results', []))} results"


async def main(live: bool) -> int:
    applied = load_env_defaults()
    print(f"Loaded .env: {', '.join(applied) if applied else 'nothing new'}\n")
    ok = True
    for var, name, purpose, url in SERVICES:
        value = os.getenv(var, "").strip()
        if not value:
            print(f"[missing] {var:16} {name}: needed for {purpose}. Get one at {url}")
            continue
        status = f"[set]     {var:16} {name} ({len(value)} characters)"
        if live:
            try:
                detail = await (check_nebius() if var == "NEBIUS_API_KEY" else check_tavily())
                status += f" -> works, {detail}"
            except (GatewayUnavailable, TavilyUnavailable) as exc:
                status += f" -> FAILED: {exc}"
                ok = False
        print(status)
    token_set = bool(os.getenv("REVIEW_API_TOKEN"))
    print("\nOptional: REVIEW_API_TOKEN is", "set (access code required)" if token_set else "not set (endpoint is open)")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="make one tiny request per configured service")
    sys.exit(asyncio.run(main(parser.parse_args().live)))
