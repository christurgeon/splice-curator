import sys
import types

import pytest

from splice_curator.auth import (
    COOKIE_NAMES,
    AuthError,
    Session,
    jwt_expiry,
    load_session,
)
from tests.helpers import make_jwt

NOW = 1_800_000_000.0


def test_jwt_expiry_reads_exp():
    assert jwt_expiry(make_jwt({"exp": 2000000000})) == 2000000000


def test_jwt_expiry_bad_token_is_none():
    assert jwt_expiry("abc") is None
    assert jwt_expiry(make_jwt({"sub": "x"})) is None
    assert jwt_expiry("a.!!!.c") is None


def test_env_session(monkeypatch):
    tok = make_jwt({"exp": int(NOW) + 3600})
    monkeypatch.setenv("SPLICE_TOKEN", tok)
    s = load_session("env", NOW, COOKIE_NAMES)
    assert s.token == tok
    assert s.cookies == {"_splice_token_prod": tok}
    assert s.authorization == "Bearer " + tok
    assert s.expires_at == int(NOW) + 3600


def test_expired_token_raises(monkeypatch):
    tok = make_jwt({"exp": int(NOW) - 1})
    monkeypatch.setenv("SPLICE_TOKEN", tok)
    with pytest.raises(AuthError) as exc:
        load_session("env", NOW, COOKIE_NAMES)
    assert tok not in str(exc.value)
    assert "expired" in str(exc.value)


def test_unreadable_token_raises(monkeypatch):
    monkeypatch.setenv("SPLICE_TOKEN", "not-a-jwt")
    with pytest.raises(AuthError) as exc:
        load_session("env", NOW, COOKIE_NAMES)
    assert "not-a-jwt" not in str(exc.value)


def test_missing_env_raises(monkeypatch):
    monkeypatch.delenv("SPLICE_TOKEN", raising=False)
    with pytest.raises(AuthError):
        load_session("env", NOW, COOKIE_NAMES)


def test_unknown_source_raises():
    with pytest.raises(AuthError):
        load_session("firefox", NOW, COOKIE_NAMES)


def test_repr_redacts_token():
    tok = "eyJ" + "secret.payload.sig"
    s = Session(token=tok, cookies={"_splice_token_prod": tok})
    assert "eyJsecret" not in repr(s)
    assert "eyJsecret" not in str(s)
    assert "_splice_token_prod" in repr(s)


def test_chrome_filters_cookie_names(monkeypatch):
    tok = make_jwt({"exp": int(NOW) + 3600})
    fake = types.ModuleType("pycookiecheat")
    calls = []

    def chrome_cookies(url):
        calls.append(url)
        return {"_splice_token_prod": tok, "XSRF-TOKEN": "x", "other": "y"}

    fake.chrome_cookies = chrome_cookies
    monkeypatch.setitem(sys.modules, "pycookiecheat", fake)
    s = load_session("chrome", NOW, ("_splice_token_prod", "XSRF-TOKEN"))
    assert s.cookies == {"_splice_token_prod": tok, "XSRF-TOKEN": "x"}
    assert s.token == tok
    assert calls == ["https://splice.com"]


def test_chrome_missing_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "pycookiecheat", None)
    with pytest.raises(AuthError) as exc:
        load_session("chrome", NOW, COOKIE_NAMES)
    assert "uv sync --extra chrome" in str(exc.value)
