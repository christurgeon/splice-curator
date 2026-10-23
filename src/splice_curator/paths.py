"""exFAT- and Windows-safe file and folder names."""

from __future__ import annotations

import re

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_MAX_EXT_BYTES = 16


def _truncate(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def safe_part(text: str, max_bytes: int = 255) -> str:
    """One path component that is legal on exFAT, Windows, macOS, and Linux."""
    name = _FORBIDDEN.sub("_", text).rstrip(" .")
    if not name:
        return "_"
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext.encode()) > _MAX_EXT_BYTES:
        stem, ext = name, ""
    else:
        ext = "." + ext
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    room = max_bytes - len(ext.encode())
    stem = _truncate(stem, room).rstrip(" .") or "_"
    return stem + ext
