"""Test helpers. Fake tokens are built at runtime; no JWT literal is stored."""

import base64
import json


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(payload: dict) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    return f"{header}.{body}.{_b64(b'fake-signature-bytes')}"
