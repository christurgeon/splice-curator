import random

from splice_curator.config import Delay
from splice_curator.throttle import Throttle

DELAYS = {
    "search": Delay(3, 6),
    "purchase": Delay(6, 12),
    "download": Delay(2, 5),
}


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make(pause_every=0, pause_seconds=60.0, clock=None):
    clock = clock or FakeClock()
    t = Throttle(DELAYS, pause_every, pause_seconds,
                 sleep=clock.sleep, monotonic=clock.monotonic, rng=random.Random(42))
    return t, clock


def test_first_wait_does_not_sleep():
    t, _ = make()
    assert t.wait("search") == 0.0


def test_wait_enforces_gap_from_last_request_of_any_kind():
    t, _ = make()
    t.wait("search")
    slept = t.wait("purchase")
    assert 6 <= slept <= 12


def test_wait_counts_elapsed_time():
    t, clock = make()
    t.wait("search")
    clock.t += 10
    assert t.wait("search") == 0


def test_pause_every_n_requests():
    t, clock = make(pause_every=3, pause_seconds=60)
    for _ in range(3):
        t.wait("search")
        clock.t += 100  # elapsed time covers the normal delay
    assert t.wait("search") >= 60


def test_unknown_kind_raises():
    t, _ = make()
    try:
        t.wait("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_backoff_honors_retry_after_and_grows():
    t, clock = make()
    assert t.backoff(1, retry_after=30) == 30
    assert clock.slept[-1] == 30
    for _ in range(20):
        assert 5 <= t.backoff(1, None) <= 10
        assert 20 <= t.backoff(3, None) <= 40
