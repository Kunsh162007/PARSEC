import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import yaml
from pydantic import BaseModel

from packages.common.envfile import load_env_defaults

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "models.yaml"
DEFAULT_BASE_URL = "https://api.studio.nebius.ai/v1"
REQUEST_TIMEOUT_SECONDS = 90.0
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class GatewayUnavailable(Exception):
    """No live model answered. Callers must report this; there is no simulated fallback."""


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class GatewayResponse(BaseModel):
    content: str
    parsed_json: Optional[Dict[str, Any]] = None
    model_used: str
    tier: str  # nano, super, ultra
    usage: TokenUsage
    cached: bool = False


def _tier_of(model: str) -> str:
    lowered = model.lower()
    return "ultra" if "ultra" in lowered else ("super" if "super" in lowered else "nano")


class LLMGateway:
    """
    Gateway for Nebius Token Factory with Nemotron 3 family routing, JSON parsing, caching and live metering.

    It only ever returns what a model actually said. With no key, or when every configured model fails,
    complete() raises GatewayUnavailable. Token counts are what the API reported (zero if it reported none),
    and a model with no listed price has unknown cost, recorded as 0.0 and never as an assumed rate.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        api_key: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        retries: int = 2,
        retry_delay: float = 1.0,
    ):
        with open(config_path or DEFAULT_CONFIG, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        if api_key is None:
            load_env_defaults()
            api_key = os.getenv("NEBIUS_API_KEY", "")
        self.api_key = api_key.strip()
        self._transport = transport
        self.retries = retries
        self.retry_delay = retry_delay
        self.base_url = (self.config.get("providers", {}).get("nebius", {}).get("base_url") or DEFAULT_BASE_URL)
        self.routing = self.config.get("routing", {})
        self.pricing = self.config.get("pricing_per_million_tokens", {})
        self.cache: Dict[str, GatewayResponse] = {}

        self.total_tokens_nano = 0
        self.total_tokens_super = 0
        self.total_tokens_ultra = 0
        self.total_cost_usd = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def calculate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        rates = self.pricing.get(model)
        if not rates:
            return 0.0
        cost = (prompt_tokens / 1_000_000.0) * rates["input"] + (completion_tokens / 1_000_000.0) * rates["output"]
        return round(cost, 6)

    def route_task(self, task: str) -> Dict[str, Any]:
        route = self.routing.get(task)
        if route is None:
            raise KeyError(f"No route configured for task '{task}' in config/models.yaml")
        return route

    async def _post(self, client: httpx.AsyncClient, model: str, messages: List[Dict[str, str]], cfg: Dict[str, Any]):
        """One model, with a few retries for the failures that are worth retrying (throttling, server errors).

        A bad key or a bad request is not retried: asking again cannot change the answer.
        """
        last = ""
        for attempt in range(self.retries + 1):
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": cfg.get("max_tokens", 1024),
                        "temperature": cfg.get("temperature", 0.0),
                    },
                )
            except httpx.HTTPError as exc:
                last = f"{model}: {type(exc).__name__}"
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    try:
                        choice = data["choices"][0]
                        content = choice["message"]["content"]
                    except (KeyError, IndexError, TypeError):
                        raise GatewayUnavailable(f"{model}: response had no completion")
                    if not (content or "").strip():
                        # Reasoning models can spend the whole token budget thinking and return no answer text.
                        raise GatewayUnavailable(
                            f"{model}: no answer text (finish_reason={choice.get('finish_reason', 'unknown')})")
                    return content, data.get("usage") or {}
                last = f"{model}: HTTP {resp.status_code}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise GatewayUnavailable(last)
            if attempt < self.retries and self.retry_delay:
                await asyncio.sleep(self.retry_delay * (attempt + 1))
        raise GatewayUnavailable(last)

    async def complete(
        self,
        task: str,
        messages: list,
        response_schema: Optional[type[BaseModel]] = None,
        use_cache: bool = True,
    ) -> GatewayResponse:
        if not self.is_configured:
            raise GatewayUnavailable("NEBIUS_API_KEY is not set. Add it to .env or the environment.")

        cfg = self.route_task(task)
        cache_key = f"{task}:{hash(json.dumps(messages, sort_keys=True))}"
        if use_cache and cache_key in self.cache:
            return self.cache[cache_key]

        candidates = [cfg["model"]]
        fallback_model = cfg.get("fallback_model")
        if fallback_model and fallback_model != cfg["model"]:
            candidates.append(fallback_model)

        errors: List[str] = []
        content: Optional[str] = None
        usage_data: Dict[str, Any] = {}
        answered_by = ""
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, transport=self._transport) as client:
            for candidate in candidates:
                try:
                    content, usage_data = await self._post(client, candidate, messages, cfg)
                    answered_by = candidate
                    break
                except GatewayUnavailable as exc:
                    errors.append(str(exc))
                except httpx.HTTPError as exc:
                    errors.append(f"{candidate}: {type(exc).__name__}")
        if content is None:
            raise GatewayUnavailable("; ".join(errors) or "no model answered")

        tier = _tier_of(answered_by)
        prompt_tokens = int(usage_data.get("prompt_tokens") or 0)
        completion_tokens = int(usage_data.get("completion_tokens") or 0)
        cost = self.calculate_cost(answered_by, prompt_tokens, completion_tokens)

        if tier == "ultra":
            self.total_tokens_ultra += prompt_tokens + completion_tokens
        elif tier == "super":
            self.total_tokens_super += prompt_tokens + completion_tokens
        else:
            self.total_tokens_nano += prompt_tokens + completion_tokens
        self.total_cost_usd += cost

        parsed_json = None
        if response_schema or "json" in task or "adjudication" in task or "inversion" in task:
            clean = content.strip()
            if clean.startswith("```json"):
                clean = clean[7:]
            if clean.endswith("```"):
                clean = clean[:-3]
            try:
                parsed_json = json.loads(clean.strip())
            except (ValueError, TypeError):
                parsed_json = None

        res = GatewayResponse(
            content=content,
            parsed_json=parsed_json,
            model_used=answered_by,
            tier=tier,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cost_usd=cost,
            ),
        )
        if use_cache:
            self.cache[cache_key] = res
        return res
