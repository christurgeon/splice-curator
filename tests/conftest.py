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


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail any test that would open a real connection (only MockTransport is allowed)."""
    import httpx

    def refuse(self, request):
        raise AssertionError(f"real network request attempted: {request.url.host}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


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


# -- fake Splice client --------------------------------------------------------

from splice_curator.client import PurchaseUncertain  # noqa: E402
from splice_curator.models import Account, AssetFiles, SearchPage  # noqa: E402
from tests.helpers import make_sample  # noqa: E402


class FakeClient:
    """In-memory stand-in for SpliceClient. Never touches the network."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.credits = 3360
        self.username = "tester"
        self.store: dict[str, object] = {}  # catalog_uuid -> Sample (the "server")
        self.owned: set[str] = set()
        self.per_page = 2
        self.errors: dict[tuple[str, str], Exception] = {}  # (method, catalog_uuid) -> exc
        self.prices: dict[str, int | None] = {}  # catalog_uuid -> price (default 1)
        self.credit_drift = 0  # extra credits the server takes per purchase
        self.account_error: Exception | None = None
        self.search_error: Exception | None = None
        self.download_error: Exception | None = None
        self._n = 0

    def add(self, sample, owned=False):
        self.store[sample.catalog_uuid] = sample
        if owned:
            self.owned.add(sample.catalog_uuid)
        return sample

    def _raise(self, method, uuid):
        exc = self.errors.pop((method, uuid), None)
        if exc:
            raise exc

    def account(self):
        self.calls.append(("account",))
        if self.account_error:
            raise self.account_error
        return Account(self.username, self.credits, "active")

    def search(self, query, category, page, limit=50, sort="popularity", licensed=None):
        self.calls.append(("search", query, category, page, licensed))
        if self.search_error:
            raise self.search_error
        if licensed:
            owned = sorted(self.owned)
            chunk = owned[(page - 1) * limit : page * limit]
            samples = [self.store[u] for u in chunk]
            for s in samples:
                s.licensed = True
            pages = max(1, -(-len(owned) // limit))
            return SearchPage(len(owned), pages, samples)
        samples = []
        for _ in range(self.per_page):
            self._n += 1
            samples.append(self.add(make_sample(self._n, category=category or "oneshot")))
        return SearchPage(self.per_page * 10, 10, samples)

    def asset_files(self, catalog_uuid):
        self.calls.append(("asset_files", catalog_uuid))
        self._raise("asset_files", catalog_uuid)
        s = self.store[catalog_uuid]
        owned = catalog_uuid in self.owned
        src = f"https://spliceproduction.s3.us-west-1.amazonaws.com/audio_samples/{s.file_hash}" if owned else None
        return AssetFiles(s.name, owned, s.preview_url, src, self.prices.get(catalog_uuid, 1))

    def purchase(self, catalog_uuid):
        self.calls.append(("purchase", catalog_uuid))
        self._raise("purchase", catalog_uuid)
        if self.credits < 1:
            raise PurchaseUncertain("no credits")
        self.credits -= (self.prices.get(catalog_uuid) or 1) + self.credit_drift
        self.owned.add(catalog_uuid)
        return True

    def download(self, url, dest):
        self.calls.append(("download", url, str(dest)))
        if self.download_error:
            raise self.download_error
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = b"RIFF" + url.encode()
        dest.write_bytes(data)
        return len(data)

    def count(self, method):
        return sum(1 for c in self.calls if c[0] == method)


@pytest.fixture
def fake_client(session):
    client = FakeClient()
    client.session = session
    return client


@pytest.fixture
def cli_env(tmp_path, fake_client, monkeypatch):
    """Config file under tmp_path and the CLI wired to the fake client."""
    from splice_curator import cli

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'data_dir = "{tmp_path / "data"}"\n'
        f'staging_dir = "{tmp_path / "staging"}"\n'
        f'splice_dir = "{tmp_path / "Splice"}"\n'
    )
    monkeypatch.setattr(cli, "build_client", lambda config, source: fake_client)
    return cfg
