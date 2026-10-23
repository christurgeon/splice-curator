import httpx
import pytest

from splice_curator.auth import AuthError, Session
from splice_curator.client import AuthRejected, SpliceClient
from splice_curator.config import load_config
from splice_curator.throttle import Throttle
from tests.helpers import make_jwt, search_item, search_response
from tests.test_client import UUID

NOW = 1_800_000_000.0
ACCOUNT_OK = {"data": {"user": {"username": "u"}, "subscription": {"credits": 1, "state": "active"}}}


def session_for(exp_in: float, tag: str = "a") -> Session:
    token = make_jwt({"exp": int(NOW + exp_in), "tag": tag})
    return Session(token=token, cookies={"_splice_token_prod": token, "XSRF-TOKEN": "xsrf"})


class Env:
    """Fake clock, recording sleep, scripted refresher, and a request log.

    The refresher returns the scripted items in order, then repeats the last one.
    """

    def __init__(self, config, initial, refreshes=(), handler=None, with_refresher=True):
        self.now = NOW
        self.sleeps: list[float] = []
        self.refresher_calls = 0
        self._refreshes = list(refreshes)
        self._last = None
        self.requests: list[httpx.Request] = []
        self._handler = handler or (lambda r: httpx.Response(200, json=ACCOUNT_OK))
        throttle = Throttle(config.delays, 0, 0.0, sleep=self._sleep)
        self.client = SpliceClient(
            initial, throttle, config,
            transport=httpx.MockTransport(self._handle),
            refresher=self._refresh if with_refresher else None,
            clock=lambda: self.now,
        )

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def _refresh(self):
        self.refresher_calls += 1
        if self._refreshes:
            self._last = self._refreshes.pop(0)
        if isinstance(self._last, Exception):
            raise self._last
        return self._last

    def _handle(self, request):
        self.requests.append(request)
        return self._handler(request)


def test_no_refresh_when_token_fresh(config):
    env = Env(config, session_for(3600))
    env.client.account()
    assert env.refresher_calls == 0


def test_refresh_swaps_token_and_headers(config, capsys):
    new = session_for(7200, "b")
    env = Env(config, session_for(100), [new])
    env.client.account()
    r = env.requests[0]
    assert r.headers["authorization"] == "Bearer " + new.token
    assert "_splice_token_prod=" + new.token in r.headers["cookie"]
    assert env.client.session is new
    assert "renewed Splice login (expires " in capsys.readouterr().err


def test_refresh_waits_for_chrome_then_succeeds(config, capsys):
    old = session_for(100)
    env = Env(config, old, [old, old, session_for(7200, "b")])
    env.client.account()
    assert env.sleeps == [config.refresh_poll_s] * 2
    assert env.refresher_calls == 3
    assert "keep a splice.com tab open (up to 15 min)" in capsys.readouterr().err


def test_refresh_gives_up_after_deadline(config):
    old = session_for(100)
    env = Env(config, old, [old])
    with pytest.raises(AuthRejected) as exc:
        env.client.account()
    assert "Chrome did not renew" in str(exc.value)
    assert sum(env.sleeps) <= config.refresh_wait_s
    assert env.requests == []


def test_waits_through_actual_expiry(config):
    old = session_for(10)
    env = Env(config, old, [old, old, session_for(7200, "b")])
    env.client.account()  # old token expires during the wait; polling continues
    assert len(env.requests) == 1


def test_refresher_auth_error_counts_as_not_yet(config):
    env = Env(config, session_for(100), [AuthError("could not read Chrome cookies (X)"), session_for(7200, "b")])
    env.client.account()
    assert env.sleeps == [config.refresh_poll_s]
    assert len(env.requests) == 1


def test_refresher_auth_error_forever_raises(config):
    env = Env(config, session_for(100), [AuthError("could not read Chrome cookies (X)")])
    with pytest.raises(AuthRejected):
        env.client.account()


def test_new_token_inside_margin_is_not_accepted(config):
    env = Env(config, session_for(100), [session_for(200, "b"), session_for(7200, "c")])
    env.client.account()
    assert env.refresher_calls == 2


def test_env_source_near_expiry_raises(config):
    env = Env(config, session_for(100), with_refresher=False)
    with pytest.raises(AuthRejected) as exc:
        env.client.account()
    assert "log in" in str(exc.value)
    assert env.requests == []


def test_401_refreshes_and_retries_once_for_search(config):
    new = session_for(7200, "b")
    responses = [httpx.Response(401), httpx.Response(200, json=search_response([search_item()]))]
    env = Env(config, session_for(3600), [new], handler=lambda r: responses.pop(0))
    page = env.client.search("x", None, 1)
    assert len(page.samples) == 1
    assert len(env.requests) == 2
    assert env.requests[1].headers["authorization"] == "Bearer " + new.token


def test_401_twice_raises(config):
    env = Env(config, session_for(3600), [session_for(7200, "b")], handler=lambda r: httpx.Response(401))
    with pytest.raises(AuthRejected):
        env.client.account()
    assert len(env.requests) == 2


def test_401_on_unexpired_token_does_not_wait_for_chrome(config):
    # A 401 on a token that has not expired is a server-side rejection: try Chrome once, then stop.
    env = Env(config, session_for(3600), [session_for(3600)], handler=lambda r: httpx.Response(401))
    with pytest.raises(AuthRejected):
        env.client.account()
    assert env.refresher_calls == 1
    assert max(env.sleeps, default=0) < config.refresh_poll_s
    assert len(env.requests) == 1


def test_401_without_refresher_raises_as_before(config):
    env = Env(config, session_for(3600), handler=lambda r: httpx.Response(401), with_refresher=False)
    with pytest.raises(AuthRejected) as exc:
        env.client.account()
    assert "log in to splice.com again" in str(exc.value)
    assert len(env.requests) == 1


def test_purchase_401_is_not_retried(config):
    env = Env(config, session_for(3600), [session_for(7200, "b")], handler=lambda r: httpx.Response(401))
    with pytest.raises(AuthRejected):
        env.client.purchase(UUID)
    assert len(env.requests) == 1
    assert env.refresher_calls == 0


def test_purchase_refreshes_proactively(config):
    new = session_for(7200, "b")
    ok = httpx.Response(200, json={"data": {"purchaseAsset": {"uuid": UUID, "licensed": True}}})
    env = Env(config, session_for(100), [new], handler=lambda r: ok)
    assert env.client.purchase(UUID) is True
    assert env.requests[0].headers["authorization"] == "Bearer " + new.token


def test_downloads_do_not_trigger_refresh(config, tmp_path):
    env = Env(config, session_for(100), handler=lambda r: httpx.Response(200, content=b"RIFF"))
    url = "https://spliceproduction.s3.us-west-1.amazonaws.com/audio_samples/abc?X-Amz-Signature=s"
    env.client.download(url, tmp_path / "a.wav")
    assert env.refresher_calls == 0


def test_refresh_messages_never_contain_token(config, capsys):
    old = session_for(100)
    new = session_for(7200, "b")
    texts = []
    env = Env(config, old, [AuthError("could not read Chrome cookies (X)"), old, new])
    env.client.account()
    texts.append(capsys.readouterr().err)
    for env in (
        Env(config, old, [old]),
        Env(config, old, with_refresher=False),
        Env(config, session_for(3600), [new], handler=lambda r: httpx.Response(401)),
    ):
        with pytest.raises(AuthRejected) as exc:
            env.client.account()
        texts.append(str(exc.value))
        texts.append(capsys.readouterr().err)
    for text in texts:
        assert "eyJ" not in text
        assert old.token not in text and new.token not in text
    assert any(texts)


def test_config_refresh_defaults(tmp_path):
    cfg = load_config(path=None)
    assert (cfg.refresh_margin_s, cfg.refresh_poll_s, cfg.refresh_wait_s) == (300, 30, 900)
    path = tmp_path / "c.toml"
    path.write_text("refresh_margin_s = 60\nrefresh_poll_s = 5\nrefresh_wait_s = 120\n")
    cfg = load_config(path)
    assert (cfg.refresh_margin_s, cfg.refresh_poll_s, cfg.refresh_wait_s) == (60, 5, 120)
