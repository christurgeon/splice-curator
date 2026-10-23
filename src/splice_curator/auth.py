"""Session token from the environment or from Chrome's cookie store.

The token is never printed, logged, or written to disk, and no error
message includes it.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field

TOKEN_COOKIE = "_splice_token_prod"
# Cookie names the site sends (docs/api-notes.md).
COOKIE_NAMES: tuple[str, ...] = (TOKEN_COOKIE, "XSRF-TOKEN")
ENV_VAR = "SPLICE_TOKEN"


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


def _chrome_cookies() -> dict[str, str]:
    try:
        import pycookiecheat
    except ImportError:
        raise AuthError("install with: uv sync --extra chrome") from None
    try:
        return dict(pycookiecheat.chrome_cookies("https://splice.com"))
    except Exception as exc:  # message from the library never holds cookie values
        raise AuthError(f"could not read Chrome cookies ({type(exc).__name__})") from None


def load_session(source: str, now: float, cookie_names: tuple[str, ...]) -> Session:
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
    if exp <= now:
        raise AuthError("token expired; log in to splice.com again")
    return Session(token=token, cookies=cookies)
