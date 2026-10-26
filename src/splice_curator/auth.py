"""Session token from the environment or from Chrome's cookie store.

The token is never printed, logged, or written to disk, and no error
message includes it.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

TOKEN_COOKIE = "_splice_token_prod"
# Cookie names the site sends (docs/api-notes.md).
COOKIE_NAMES: tuple[str, ...] = (TOKEN_COOKIE, "XSRF-TOKEN")
ENV_VAR = "SPLICE_TOKEN"
PROFILE_ENV_VAR = "SPLICE_CHROME_PROFILE"
CHROME_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


class AuthError(Exception):
    pass


def jwt_expiry(token: str) -> int | None:
    """Return the JWT ``exp`` claim, or None on any parse problem."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims["exp"]
        return int(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


@dataclass
class Session:
    token: str = field(repr=False)
    cookies: dict[str, str] = field(repr=False)

    def __repr__(self) -> str:
        return f"Session(token=<redacted>, cookies={sorted(self.cookies)})"

    __str__ = __repr__

    @property
    def authorization(self) -> str:
        return "Bearer " + self.token

    @property
    def expires_at(self) -> int | None:
        return jwt_expiry(self.token)


def chrome_cookie_file(chrome_dir: Path) -> Path:
    """Cookie DB of the profile Chrome used last, unless SPLICE_CHROME_PROFILE names one."""
    profile = os.environ.get(PROFILE_ENV_VAR, "").strip()
    if not profile:
        try:
            state = json.loads((chrome_dir / "Local State").read_text())
            profile = state["profile"]["last_used"]
        except Exception:
            profile = "Default"
    network = chrome_dir / profile / "Network" / "Cookies"
    return network if network.is_file() else chrome_dir / profile / "Cookies"


def _chrome_cookies() -> dict[str, str]:
    try:
        import pycookiecheat
    except ImportError:
        raise AuthError("install with: uv sync --extra chrome") from None
    try:
        return dict(pycookiecheat.chrome_cookies("https://splice.com", cookie_file=chrome_cookie_file(CHROME_DIR)))
    except Exception as exc:  # message from the library never holds cookie values
        raise AuthError(f"could not read Chrome cookies ({type(exc).__name__})") from None


def load_session(source: str, now: float, cookie_names: tuple[str, ...], allow_expired: bool = False) -> Session:
    """Read the Splice token. ``allow_expired`` keeps an expired token for a client that can renew it."""
    if source == "env":
        token = os.environ.get(ENV_VAR, "").strip()
        if not token:
            raise AuthError(f"{ENV_VAR} is not set; copy the {TOKEN_COOKIE} cookie value from splice.com")
        cookies = {TOKEN_COOKIE: token}
    elif source == "chrome":
        all_cookies = _chrome_cookies()
        cookies = {k: v for k, v in all_cookies.items() if k in cookie_names}
        token = cookies.get(TOKEN_COOKIE, "")
        if not token:
            raise AuthError("no Splice session in Chrome; log in to splice.com again")
    else:
        raise AuthError(f"unknown token source: {source!r} (use env or chrome)")

    exp = jwt_expiry(token)
    if exp is None:
        raise AuthError("token is not a readable JWT; copy it again from splice.com")
    if exp <= now and not allow_expired:
        raise AuthError("token expired; log in to splice.com again")
    return Session(token=token, cookies=cookies)
