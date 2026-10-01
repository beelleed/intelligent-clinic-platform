"""Small in-memory rate limiter for the public portfolio demo."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Literal


SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0
    reason: Literal["minute", "day"] | None = None


class InMemoryRateLimiter:
    """Enforce minute and UTC-day limits for one application instance."""

    def __init__(self) -> None:
        self._minute_requests: dict[str, deque[float]] = defaultdict(deque)
        self._daily_requests: dict[tuple[str, int], int] = defaultdict(int)
        self._lock = threading.Lock()

    def check(
        self,
        client_key: str,
        *,
        per_minute: int,
        per_day: int,
        now: float | None = None,
    ) -> RateLimitDecision:
        current_time = time.time() if now is None else now
        day_number = int(current_time // SECONDS_PER_DAY)

        with self._lock:
            minute_requests = self._minute_requests[client_key]
            cutoff = current_time - 60
            while minute_requests and minute_requests[0] <= cutoff:
                minute_requests.popleft()

            if len(minute_requests) >= per_minute:
                retry_after = max(
                    1,
                    int(60 - (current_time - minute_requests[0])) + 1,
                )
                return RateLimitDecision(False, retry_after, "minute")

            daily_key = (client_key, day_number)
            if self._daily_requests[daily_key] >= per_day:
                next_day = (day_number + 1) * SECONDS_PER_DAY
                return RateLimitDecision(
                    False,
                    max(1, int(next_day - current_time) + 1),
                    "day",
                )

            minute_requests.append(current_time)
            self._daily_requests[daily_key] += 1
            self._remove_stale_days(day_number)
            return RateLimitDecision(True)

    def reset(self) -> None:
        """Clear counters for tests and local development."""
        with self._lock:
            self._minute_requests.clear()
            self._daily_requests.clear()

    def _remove_stale_days(self, current_day: int) -> None:
        stale_keys = [
            key for key in self._daily_requests if key[1] < current_day
        ]
        for key in stale_keys:
            del self._daily_requests[key]


chat_rate_limiter = InMemoryRateLimiter()
