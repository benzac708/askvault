"""D32's rate limiter, tested against the properties that make it safe.

The clock is injected everywhere, so every test is deterministic and no test
sleeps. That is the reason `RateLimiter` takes `clock` at all: refill and
day-rollover behaviour is exactly what a fake clock is for.
"""

from datetime import UTC, datetime

import pytest

from app.core.config import settings
from app.core.limits import MAX_TRACKED_IPS, RateLimiter, RateLimitExceeded


class Clock:
    def __init__(self, epoch: float) -> None:
        self.now = epoch

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# 2026-09-26 12:00:00 UTC, a fixed point so day-rollover tests are readable.
NOON = datetime(2026, 9, 26, 12, 0, tzinfo=UTC).timestamp()


def limiter(clock: Clock, *, ip: int = 10, minute: int = 100, day: int = 1000) -> RateLimiter:
    return RateLimiter(
        per_ip_per_minute=ip, global_per_minute=minute, global_per_day=day, clock=clock
    )


def test_the_per_ip_ceiling_rejects_the_eleventh_request_in_a_minute():
    clock = Clock(NOON)
    rl = limiter(clock, ip=10)
    for _ in range(10):
        rl.check("1.2.3.4")
    with pytest.raises(RateLimitExceeded) as exc:
        rl.check("1.2.3.4")
    assert exc.value.scope == "per-ip"
    assert exc.value.retry_after >= 1


def test_the_per_ip_ceiling_is_per_ip_not_global():
    """One caller exhausting their budget must not affect the next caller."""
    clock = Clock(NOON)
    rl = limiter(clock, ip=2)
    rl.check("1.1.1.1")
    rl.check("1.1.1.1")
    with pytest.raises(RateLimitExceeded):
        rl.check("1.1.1.1")
    assert rl.check("2.2.2.2") is None


def test_a_rejected_ip_does_not_consume_the_global_budget():
    """Ordering is load-bearing: per-IP is checked before the global bucket.

    If it were the other way round, one abusive client could burn the shared
    budget and lock out every honest visitor, which turns a nuisance into a
    denial of service.

    Each honest client below is a *different* address, so the per-IP ceiling
    cannot be what stops the last one. Only the global bucket can be.
    """
    clock = Clock(NOON)
    rl = limiter(clock, ip=2, minute=5)
    rl.check("abuser")
    rl.check("abuser")
    with pytest.raises(RateLimitExceeded):
        rl.check("abuser")

    # The abuser took 2 of 5 global tokens. A third request from the same address
    # was rejected, and rejection cost the shared budget nothing.
    for i in range(3):
        assert rl.check(f"polite-{i}") is None
    with pytest.raises(RateLimitExceeded) as exc:
        rl.check("polite-3")
    assert exc.value.scope == "global-minute"


def test_the_global_minute_ceiling_bounds_the_whole_fleet():
    """Many IPs cannot exceed the global ceiling between them.

    This is the ceiling that actually protects the provider quota, because
    X-Forwarded-For is spoofable and a rotating client id defeats the per-IP one.
    """
    clock = Clock(NOON)
    rl = limiter(clock, ip=100, minute=15, day=1000)
    for i in range(15):
        rl.check(f"10.0.0.{i}")
    with pytest.raises(RateLimitExceeded) as exc:
        rl.check("10.0.0.200")
    assert exc.value.scope == "global-minute"


def test_the_daily_ceiling_is_account_wide_not_per_ip():
    clock = Clock(NOON)
    rl = limiter(clock, ip=100, minute=100, day=3)
    for i in range(3):
        rl.check(f"10.0.0.{i}")
    with pytest.raises(RateLimitExceeded) as exc:
        rl.check("10.0.0.99")
    assert exc.value.scope == "daily"


def test_the_daily_ceiling_reports_a_retry_hint_of_at_most_a_day():
    clock = Clock(NOON)
    rl = limiter(clock, ip=100, minute=100, day=1)
    rl.check("a")
    with pytest.raises(RateLimitExceeded) as exc:
        rl.check("b")
    assert exc.value.scope == "daily"
    assert 1 <= exc.value.retry_after <= 86_400


def test_the_daily_ceiling_resets_at_midnight_utc():
    just_before_midnight = datetime(2026, 9, 26, 23, 59, tzinfo=UTC).timestamp()
    clock = Clock(just_before_midnight)
    rl = limiter(clock, ip=100, minute=100, day=2)
    rl.check("a")
    rl.check("b")
    with pytest.raises(RateLimitExceeded) as exc:
        rl.check("c")
    assert exc.value.retry_after <= 60

    clock.advance(61)  # one minute later it is the 27th
    assert rl.check("c") is None


def test_the_minute_ceilings_refill_so_a_backed_off_caller_recovers():
    clock = Clock(NOON)
    rl = limiter(clock, ip=2, minute=100)
    rl.check("a")
    rl.check("a")
    with pytest.raises(RateLimitExceeded):
        rl.check("a")

    clock.advance(30)  # half a window refills exactly one token
    assert rl.check("a") is None
    with pytest.raises(RateLimitExceeded):
        rl.check("a")


def test_refill_never_exceeds_capacity():
    """A bucket left alone for a day holds a minute's worth, not a day's."""
    clock = Clock(NOON)
    rl = limiter(clock, ip=3, minute=1000)
    rl.check("a")
    clock.advance(86_400)
    for _ in range(3):
        assert rl.check("a") is None
    with pytest.raises(RateLimitExceeded):
        rl.check("a")


def test_tracked_ips_are_bounded_so_spoofed_headers_cannot_leak_memory():
    """`MAX_TRACKED_IPS` is the only thing standing between a scanner and a leak."""
    clock = Clock(NOON)
    rl = limiter(clock, ip=10_000, minute=10_000, day=10_000)
    for i in range(MAX_TRACKED_IPS + 500):
        rl.check(f"10.0.{i // 256}.{i % 256}")
    assert rl.tracked_ips <= MAX_TRACKED_IPS


def test_the_shipped_ceilings_sit_under_the_providers_own_limits():
    """D32's entire rationale, as an assertion.

    OpenRouter's unfunded free tier is 20 requests per minute and 50 per day,
    account-wide. If either of these ceilings is ever raised past that, the app
    stops failing locally and starts failing at the provider, which costs quota
    and returns an opaque error to a visitor. This test is the tripwire.
    """
    assert settings.rate_limit_per_ip_per_minute <= 20
    assert settings.rate_limit_global_per_minute <= 20
    assert settings.rate_limit_global_per_day <= 50


def test_the_per_ip_ceiling_is_under_the_global_ceiling():
    """Otherwise the per-IP tier is dead configuration: nothing would ever reach it."""
    assert settings.rate_limit_per_ip_per_minute < settings.rate_limit_global_per_minute
