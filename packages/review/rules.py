"""The Fine Print rules on the server, run by the same engine.js the browser uses (through Node).

There is one copy of the rules. The browser loads apps/review/engine.js directly; the API pipes documents to
rules_runner.js, which loads that same file. Offsets come back as Unicode code points into each document's text
after line endings are normalised to "\\n", so text[start:end] in Python is the flagged passage."""
import asyncio
import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

RUNNER = Path(__file__).with_name("rules_runner.js")
NODE_TIMEOUT_S = 30
# Node processes at once, so a burst of free calls cannot take every worker thread. Each needs about 95 MB at the
# largest request the API accepts; set RULES_CONCURRENCY=2 on a 512 MB host.
MAX_CONCURRENT_RUNS = max(1, int(os.getenv("RULES_CONCURRENCY", "4") or 4))
_runs = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
_FINDING_FIELDS = ("id", "lens", "severity", "title", "why", "ask", "detail", "count", "context")


class RulesUnavailable(RuntimeError):
    """The rules could not run on this server (Node.js missing, or the engine failed)."""


def normalize_text(text: str) -> str:
    """The browser engine's line-ending rule, applied before offsets are counted."""
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


def _node_binary() -> Optional[str]:
    return os.getenv("NODE_BINARY") or shutil.which("node")


def _call(request: Dict[str, Any]) -> Dict[str, Any]:
    node = _node_binary()
    if not node:
        raise RulesUnavailable("The rules need Node.js on the server, and it was not found. Install Node 18+ "
                               "or set NODE_BINARY.")
    try:
        proc = subprocess.run(
            [node, str(RUNNER)], input=json.dumps(request), capture_output=True, text=True,
            encoding="utf-8", timeout=NODE_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RulesUnavailable(f"The rules took longer than {NODE_TIMEOUT_S} seconds and were stopped.") from exc
    if proc.returncode != 0:
        raise RulesUnavailable(f"The rules engine failed: {proc.stderr.strip()[:300] or 'no message'}")
    return json.loads(proc.stdout)


@lru_cache(maxsize=1)
def _categories() -> tuple:
    return tuple(_call({"op": "categories"})["categories"])


def categories() -> List[Dict[str, str]]:
    """Every due diligence category the engine knows, integrity first: id, name, description."""
    return [dict(c) for c in _categories()]


def _finding(raw: Dict[str, Any], documents: List[Dict[str, str]]) -> Dict[str, Any]:
    spans = [{**s, "text": documents[s["doc"]]["text"][s["start"]:s["end"]]} for s in raw.get("spans", [])]
    return {"source": "rules", **{k: raw.get(k) for k in _FINDING_FIELDS}, "spans": spans}


async def run_rules(documents: List[Dict[str, str]], lenses: List[str], as_of: Optional[str] = None) -> Dict[str, Any]:
    """Findings for the given categories (integrity always runs). as_of (YYYY-MM-DD) fixes "today" for date checks."""
    docs = [{"name": str(d.get("name", "")), "text": normalize_text(d.get("text", ""))} for d in documents]
    async with _runs:
        out = await asyncio.to_thread(_call, {"op": "analyze", "documents": docs, "lenses": list(lenses),
                                              "today": as_of})
    return {"findings": [_finding(f, docs) for f in out["findings"]], "stats": out["stats"]}
