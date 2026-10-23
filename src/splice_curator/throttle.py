"""One shared throttle for every request to Splice."""

from __future__ import annotations

import random
import time
from typing import Callable

from .config import Delay


class Throttle:
    """Keeps a random minimum gap between requests of any kind.

    The gap is drawn from the delay of the request about to be sent, and
    time already elapsed since the previous request counts toward it.
    """

    def __init__(
        self,
        delays: dict[str, Delay],
        pause_every: int,
        pause_seconds: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        backoff_base_s: float = 5.0,
    ) -> None:
        self.delays = dict(delays)
        self.pause_every = pause_every
        self.pause_seconds = pause_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._rng = rng or random.Random()
        self._last: float | None = None
        self._count = 0
        self.backoff_base_s = backoff_base_s

    def wait(self, kind: str) -> float:
        """Sleep as needed before a request of ``kind``; return seconds slept."""
        delay = self.delays[kind]
        slept = 0.0
        if self._last is not None:
            drawn = self._rng.uniform(delay.min_s, delay.max_s)
            elapsed = self._monotonic() - self._last
            slept = max(0.0, drawn - elapsed)
            if self.pause_every and self._count % self.pause_every == 0:
                slept = max(slept, self.pause_seconds)
            if slept > 0:
                self._sleep(slept)
        self._count += 1
        self._last = self._monotonic()
        return slept

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        """Sleep before retry ``attempt`` (1-based); return seconds slept."""
        if retry_after is not None and retry_after >= 0:
            seconds = float(retry_after)
        else:
            base = self.backoff_base_s * 2 ** (attempt - 1)
            seconds = base + self._rng.uniform(0, base)
        self._sleep(seconds)
        self._last = self._monotonic()
        return seconds
