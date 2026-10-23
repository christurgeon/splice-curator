import random

import pytest

from splice_curator.auth import Session
from splice_curator.config import load_config
from splice_curator.throttle import Throttle
from tests.helpers import make_jwt


class SpyThrottle(Throttle):
    """Real throttle logic with a no-op sleep; records every call."""

    def __init__(self, delays):
        super().__init__(delays, 0, 0.0, sleep=lambda s: None, rng=random.Random(1))
        self.waits: list[str] = []
        self.backoffs: list[tuple[int, float | None]] = []

    def wait(self, kind):
        self.waits.append(kind)
        return super().wait(kind)

    def backoff(self, attempt, retry_after):
        self.backoffs.append((attempt, retry_after))
        return super().backoff(attempt, retry_after)


@pytest.fixture
def token():
    return make_jwt({"exp": 4_000_000_000})


@pytest.fixture
def session(token):
    return Session(token=token, cookies={"_splice_token_prod": token, "XSRF-TOKEN": "xsrf"})


@pytest.fixture
def config(tmp_path):
    return load_config(path=None, overrides={
        "data_dir": tmp_path / "data",
        "staging_dir": tmp_path / "staging",
        "splice_dir": tmp_path / "Splice",
    })


@pytest.fixture
def throttle(config):
    return SpyThrottle(config.delays)
