"""Deterministic tests for the in-memory public demo rate limiter."""

from app.rate_limit import InMemoryRateLimiter


def test_minute_limit_and_retry_window() -> None:
    limiter = InMemoryRateLimiter()

    assert limiter.check("client-a", per_minute=2, per_day=30, now=100).allowed
    assert limiter.check("client-a", per_minute=2, per_day=30, now=101).allowed

    blocked = limiter.check(
        "client-a",
        per_minute=2,
        per_day=30,
        now=102,
    )
    assert blocked.allowed is False
    assert blocked.retry_after == 59
    assert blocked.reason == "minute"

    assert limiter.check("client-a", per_minute=2, per_day=30, now=161).allowed


def test_daily_limit() -> None:
    limiter = InMemoryRateLimiter()

    assert limiter.check("client-a", per_minute=10, per_day=2, now=100).allowed
    assert limiter.check("client-a", per_minute=10, per_day=2, now=101).allowed

    blocked = limiter.check(
        "client-a",
        per_minute=10,
        per_day=2,
        now=102,
    )
    assert blocked.allowed is False
    assert blocked.retry_after > 0
    assert blocked.reason == "day"


def test_clients_have_independent_limits() -> None:
    limiter = InMemoryRateLimiter()

    assert limiter.check("client-a", per_minute=1, per_day=1, now=100).allowed
    assert limiter.check("client-b", per_minute=1, per_day=1, now=100).allowed
    assert not limiter.check(
        "client-a",
        per_minute=1,
        per_day=1,
        now=101,
    ).allowed
