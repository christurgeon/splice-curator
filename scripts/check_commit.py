"""Commit guard: block audio, databases, JWTs, and the owner's username.

Run by .githooks/pre-commit on staged files, or on every tracked file with
``--all``. The username is read from the uncommitted file ``.local/username``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BLOCKED_EXTENSIONS = {
    ".wav", ".aif", ".aiff", ".mp3", ".flac", ".asd",
    ".sqlite", ".sqlite3", ".db",
}
BLOCKED_NAMES = {".env"}
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
REPO_ROOT = Path(__file__).resolve().parent.parent
USERNAME_FILE = REPO_ROOT / ".local" / "username"


def check_paths(paths: list[Path], username: str | None) -> list[str]:
    """Return one problem message per bad file; an empty list means OK."""
    problems: list[str] = []
    needle = username.strip().lower() if username and username.strip() else None
    for path in paths:
        if path.name in BLOCKED_NAMES or path.suffix.lower() in BLOCKED_EXTENSIONS:
            problems.append(f"{path}: blocked file type")
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary: checked by extension only
        if JWT_RE.search(text):
            problems.append(f"{path}: contains something that looks like a JWT")
            continue
        if needle and needle in text.lower():
            problems.append(f"{path}: contains the owner's username")
    return problems


def _git_files(all_files: bool) -> list[Path]:
    if all_files:
        cmd = ["git", "ls-files"]
    else:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line]


def _read_username() -> str | None:
    if USERNAME_FILE.is_file():
        return USERNAME_FILE.read_text(encoding="utf-8").strip() or None
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    problems = check_paths(_git_files("--all" in args), _read_username())
    for problem in problems:
        print(f"commit guard: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
