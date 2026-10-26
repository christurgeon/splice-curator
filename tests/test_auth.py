import sys
import types

import pytest

from splice_curator import auth
from splice_curator.auth import (
    COOKIE_NAMES,
    AuthError,
    Session,
    chrome_cookie_file,
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


def test_expired_token_allowed_when_it_can_be_renewed(monkeypatch):
    tok = make_jwt({"exp": int(NOW) - 1})
    monkeypatch.setenv("SPLICE_TOKEN", tok)
    s = load_session("env", NOW, COOKIE_NAMES, allow_expired=True)
    assert s.token == tok and s.expires_at == int(NOW) - 1
    monkeypatch.setenv("SPLICE_TOKEN", "not-a-jwt")
    with pytest.raises(AuthError):
        load_session("env", NOW, COOKIE_NAMES, allow_expired=True)


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

    def chrome_cookies(url, cookie_file=None):
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


def test_chrome_cookie_file_uses_last_used_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("SPLICE_CHROME_PROFILE", raising=False)
    (tmp_path / "Local State").write_text('{"profile": {"last_used": "Profile 1"}}')
    assert chrome_cookie_file(tmp_path) == tmp_path / "Profile 1" / "Cookies"


def test_chrome_cookie_file_prefers_network_cookies(tmp_path, monkeypatch):
    monkeypatch.delenv("SPLICE_CHROME_PROFILE", raising=False)
    (tmp_path / "Default" / "Network").mkdir(parents=True)
    (tmp_path / "Default" / "Network" / "Cookies").write_text("")
    assert chrome_cookie_file(tmp_path) == tmp_path / "Default" / "Network" / "Cookies"


def test_chrome_cookie_file_env_override_and_default(tmp_path, monkeypatch):
    (tmp_path / "Local State").write_text("not json")
    monkeypatch.delenv("SPLICE_CHROME_PROFILE", raising=False)
    assert chrome_cookie_file(tmp_path) == tmp_path / "Default" / "Cookies"
    monkeypatch.setenv("SPLICE_CHROME_PROFILE", "Profile 3")
    assert chrome_cookie_file(tmp_path) == tmp_path / "Profile 3" / "Cookies"


def test_load_session_passes_profile_cookie_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SPLICE_CHROME_PROFILE", raising=False)
    (tmp_path / "Local State").write_text('{"profile": {"last_used": "Profile 1"}}')
    monkeypatch.setattr(auth, "CHROME_DIR", tmp_path)
    tok = make_jwt({"exp": int(NOW) + 3600})
    seen = []
    fake = types.ModuleType("pycookiecheat")

    def chrome_cookies(url, cookie_file=None):
        seen.append(cookie_file)
        return {"_splice_token_prod": tok}

    fake.chrome_cookies = chrome_cookies
    monkeypatch.setitem(sys.modules, "pycookiecheat", fake)
    load_session("chrome", NOW, COOKIE_NAMES)
    assert seen == [tmp_path / "Profile 1" / "Cookies"]
