"""Protects the paid Fine Print endpoint: optional access code, per-client rate limit, daily spend cap.

State is in memory, so limits are per server process. That is enough to stop a casual runaway or a
single abusive client from spending the API key, but it is not a substitute for gateway-level quotas.
"""
import hmac
import math
import os
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, Optional, Tuple

DEFAULT_PER_MINUTE = 6
DEFAULT_DAILY_BUDGET_USD = 2.0
WINDOW_SECONDS = 60
SECONDS_PER_DAY = 86_400


def _env_number(name: str, default: float, cast: Callable[[str], float]) -> float:
    try:
        value = cast(os.getenv(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default


class ReviewGuard:
    def __init__(
        self,
        token: str = "",
        per_minute: int = DEFAULT_PER_MINUTE,
        daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._token = token or ""
        self.per_minute = per_minute
        self.daily_budget_usd = daily_budget_usd
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._spend_day = -1
        self._spent = 0.0

    @classmethod
    def from_env(cls) -> "ReviewGuard":
        return cls(
            token=os.getenv("REVIEW_API_TOKEN", ""),
            per_minute=int(_env_number("REVIEW_RATE_PER_MINUTE", DEFAULT_PER_MINUTE, int)),
            daily_budget_usd=_env_number("REVIEW_DAILY_BUDGET_USD", DEFAULT_DAILY_BUDGET_USD, float),
        )

    @property
    def auth_required(self) -> bool:
        return bool(self._token)

    def token_ok(self, provided: Optional[str]) -> bool:
        if not self._token:
            return True
        if not provided:
            return False
        return hmac.compare_digest(provided.encode("utf-8"), self._token.encode("utf-8"))

    def rate_ok(self, client_id: str) -> Tuple[bool, int]:
        """(allowed, seconds to wait). Every attempt counts, including ones later rejected for a bad token."""
        now = self._clock()
        hits = self._hits[client_id]
        while hits and now - hits[0] >= WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False, max(1, math.ceil(WINDOW_SECONDS - (now - hits[0])))
        hits.append(now)
        return True, 0

    def _roll_day(self) -> None:
        day = int(self._clock() // SECONDS_PER_DAY)
        if day != self._spend_day:
            self._spend_day = day
            self._spent = 0.0

    def budget_ok(self) -> bool:
        self._roll_day()
        return self._spent < self.daily_budget_usd

    def record_cost(self, usd: float) -> None:
        self._roll_day()
        self._spent += max(0.0, float(usd))

    def status(self) -> Dict[str, object]:
        self._roll_day()
        return {
            "auth_required": self.auth_required,
            "per_minute": self.per_minute,
            "budget_remaining_usd": round(max(0.0, self.daily_budget_usd - self._spent), 6),
        }
