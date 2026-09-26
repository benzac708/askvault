"""D32: three rate-limit ceilings, all set strictly under the provider's own.

The app must exhaust its own budget *before* OpenRouter does. That ordering is
the entire point of doing this in-app rather than leaving it to the provider:
a visitor who trips our limit gets a clean 429 carrying a retry hint, whereas a
visitor who trips the provider's gets a 502 out of a paid account and a burnt
quota slot. Failed attempts still count against the daily cap, so leaking one
is not free.

Ceilings, unfunded free tier:

    per IP   10 per minute
    global   15 per minute
    global   40 per day

Provider: 20 per minute, 50 per day, both account-wide. Every ceiling below
sits under its provider counterpart, which `tests/test_limits.py` asserts so a
future change to either side is caught rather than assumed.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Bounded on purpose. Per-IP state is attacker-controlled: without a cap, a
# request that rotates a spoofed client id grows this map forever, and a public
# endpoint that leaks memory is a denial of service with no attacker sophistication.
MAX_TRACKED_IPS = 4096


class RateLimitExceeded(Exception):
    """A ceiling was hit. Carries the honest retry hint in whole seconds."""

    def __init__(self, scope: str, retry_after: int) -> None:
        super().__init__(f"rate limit exceeded: {scope}")
        self.scope = scope
        self.retry_after = max(1, retry_after)


class _Bucket:
    """Token bucket, refilling continuously.

    Continuous refill rather than a fixed window, so a caller that backs off
    recovers smoothly instead of waiting for an arbitrary boundary. At demo
    scale the arithmetic cost is irrelevant and the behaviour is kinder.
    """

    __slots__ = ("_capacity", "_clock", "_rate", "_tokens", "_updated")

    def __init__(self, capacity: int, per_minute: int, clock: Callable[[], float]) -> None:
        self._capacity = float(capacity)
        self._rate = per_minute / 60.0
        self._clock = clock
        self._tokens = float(capacity)
        self._updated = clock()

    def take(self) -> tuple[bool, int]:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True, 0
        return False, int((1.0 - self._tokens) / self._rate) + 1


@dataclass(slots=True)
class _Day:
    day: str
    used: int = 0


def _today(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).date().isoformat()


def _seconds_until_midnight(epoch: float) -> int:
    now = datetime.fromtimestamp(epoch, tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight + timedelta(days=1) - now).total_seconds()))


class RateLimiter:
    """Thread-safe enough for the deployment shape, and honest about it.

    The in-flight check-and-take is not atomic, so concurrent requests can
    overshoot a ceiling by roughly the number of racing threads. That is an
    accepted trade at one replica and demo scale: a strict global would need a
    lock around every request, and the overshoot is bounded and small. If the
    quota ever became the thing standing between the app and a bill, the daily
    counter is the one to make exact, and a lock or a Redis-backed counter is
    the fix. Documented rather than hidden.
    """

    def __init__(
        self,
        per_ip_per_minute: int = 10,
        global_per_minute: int = 15,
        global_per_day: int = 40,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._per_ip_ceiling = per_ip_per_minute
        self._daily_ceiling = global_per_day
        self._global = _Bucket(global_per_minute, global_per_minute, clock)
        self._clock = clock
        self._day = _Day(day=_today(clock()))
        # LRU: rotation is bounded, and the *global* bucket is what actually
        # protects the quota if someone rotates ids to dodge the per-IP one.
        self._ips: OrderedDict[str, _Bucket] = OrderedDict()

    @property
    def tracked_ips(self) -> int:
        return len(self._ips)

    def check(self, client_id: str) -> None:
        """Raise RateLimitExceeded, or return None. One call, three ceilings.

        Ordered most-specific to broadest, and the order is load-bearing: a
        single abusive IP is rejected without consuming global budget, so one
        client cannot starve everyone else. `tests/test_limits.py` pins this.
        """
        bucket = self._ips.get(client_id)
        if bucket is None:
            bucket = _Bucket(self._per_ip_ceiling, self._per_ip_ceiling, self._clock)
            self._ips[client_id] = bucket
            if len(self._ips) > MAX_TRACKED_IPS:
                self._ips.popitem(last=False)
        else:
            self._ips.move_to_end(client_id)

        allowed, retry = bucket.take()
        if not allowed:
            raise RateLimitExceeded("per-ip", retry)

        allowed, retry = self._global.take()
        if not allowed:
            raise RateLimitExceeded("global-minute", retry)

        today = _today(self._clock())
        if today != self._day.day:
            self._day = _Day(day=today)
        if self._day.used >= self._daily_ceiling:
            raise RateLimitExceeded("daily", _seconds_until_midnight(self._clock()))
        self._day.used += 1
