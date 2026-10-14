"""Protects the paid endpoints: optional access code, per-client rate limit, daily spend cap.

State is in memory, so limits are per server process. That is enough to stop a casual runaway or a
single abusive client from spending the API key, but it is not a substitute for gateway-level quotas.

Spend is reserved before a paid call and settled after it, so requests running at the same time cannot
all pass the budget check before any of them has recorded a cost.
"""
import hmac
import math
import os
import threading
import time
from collections import defaultdict, deque
from types import TracebackType
from typing import Callable, Deque, Dict, Optional, Tuple, Type

DEFAULT_PER_MINUTE = 6
DEFAULT_DAILY_BUDGET_USD = 2.0
DEFAULT_RESERVATION_USD = 0.05  # held per request until its real cost is known
WINDOW_SECONDS = 60
SECONDS_PER_DAY = 86_400
MONEY_DIGITS = 9  # rounding for budget comparisons, so 10 x 0.1 counts as exactly 1.0


def _env_number(name: str, default: float, cast: Callable[[str], float]) -> float:
    try:
        value = cast(os.getenv(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default


class Reservation:
    """Budget held for one paid request. record() charges what was really spent; leaving the with-block
    (or close()) frees the hold, whether the request succeeded or failed."""

    def __init__(self, guard: "ReviewGuard", amount: float) -> None:
        self._guard = guard
        self.amount = amount
        self._closed = False

    def record(self, usd: float) -> None:
        self._guard.record_cost(usd)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._guard._release(self.amount)

    def __enter__(self) -> "Reservation":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


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
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._last_prune = clock()
        self._spend_day = -1
        self._spent = 0.0
        self._reserved = 0.0

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
        with self._lock:
            now = self._clock()
            self._prune_idle(now)
            hits = self._hits[client_id]
            while hits and now - hits[0] >= WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False, max(1, math.ceil(WINDOW_SECONDS - (now - hits[0])))
            hits.append(now)
            return True, 0

    def _prune_idle(self, now: float) -> None:
        """Forget clients with no request inside the window, at most once per window (caller holds the lock)."""
        if now - self._last_prune < WINDOW_SECONDS:
            return
        self._last_prune = now
        idle = [key for key, hits in self._hits.items() if not hits or now - hits[-1] >= WINDOW_SECONDS]
        for key in idle:
            del self._hits[key]

    def _roll_day(self) -> None:
        day = int(self._clock() // SECONDS_PER_DAY)
        if day != self._spend_day:
            self._spend_day = day
            self._spent = 0.0

    def _committed(self) -> float:
        return round(self._spent + self._reserved, MONEY_DIGITS)

    def budget_ok(self) -> bool:
        with self._lock:
            self._roll_day()
            return self._committed() < self.daily_budget_usd

    def reserve(self, estimate_usd: float = DEFAULT_RESERVATION_USD) -> Optional[Reservation]:
        """Hold an estimated amount before a paid call, or None when spend plus holds already reach the budget."""
        amount = max(0.0, float(estimate_usd))
        with self._lock:
            self._roll_day()
            if self._committed() >= self.daily_budget_usd:
                return None
            self._reserved += amount
        return Reservation(self, amount)

    def _release(self, amount: float) -> None:
        with self._lock:
            self._reserved = max(0.0, round(self._reserved - amount, MONEY_DIGITS))

    def record_cost(self, usd: float) -> None:
        with self._lock:
            self._roll_day()
            self._spent += max(0.0, float(usd))

    def status(self) -> Dict[str, object]:
        with self._lock:
            self._roll_day()
            return {
                "auth_required": self.auth_required,
                "per_minute": self.per_minute,
                "budget_remaining_usd": round(max(0.0, self.daily_budget_usd - self._committed()), 6),
            }
