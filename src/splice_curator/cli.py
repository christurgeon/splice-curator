"""Command-line entry point: ``splice-curator``."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from .auth import COOKIE_NAMES, AuthError, load_session
from .budget import GROUPS, get_group
from .catalog import Catalog
from .client import AuthRejected, SpliceClient, SpliceError
from .config import DEFAULT_CONFIG_PATH, DELAY_KINDS, Config, Delay, load_config
from .throttle import Throttle

EXIT_OK, EXIT_ERROR, EXIT_AUTH = 0, 1, 2


def build_client(config: Config, source: str) -> SpliceClient:
    session = load_session(source, time.time(), COOKIE_NAMES)
    throttle = Throttle(
        config.delays,
        config.pause_every,
        config.pause_seconds,
        backoff_base_s=config.backoff_base_s,
    )
    return SpliceClient(session, throttle, config)


def _parse_delay(text: str) -> tuple[str, Delay]:
    try:
        kind, rng = text.split("=", 1)
        lo, hi = rng.split(":", 1)
        kind = kind.strip()
        if kind not in DELAY_KINDS:
            raise ValueError
        return kind, Delay(float(lo), float(hi))
    except ValueError:
        raise ValueError(
            f"bad --delay {text!r}; use KIND=MIN:MAX with KIND in {', '.join(DELAY_KINDS)}"
        ) from None


def load_cli_config(args: argparse.Namespace) -> Config:
    overrides: dict = {}
    if args.max_per_day is not None:
        overrides["max_purchases_per_day"] = args.max_per_day
    if args.delay:
        overrides["delays"] = dict(_parse_delay(d) for d in args.delay)
    return load_config(Path(args.config), overrides)


def _log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


# -- commands ------------------------------------------------------------------


def cmd_auth(args, config: Config) -> int:
    client = build_client(config, args.source)
    account = client.account()
    exp = client.session.expires_at
    if exp:
        left = (exp - time.time()) / 3600
        print(f"token: present, expires {datetime.fromtimestamp(exp):%Y-%m-%d %H:%M} ({left:.1f} h left)")
    print(f"user: {account.username}")
    print(f"subscription: {account.state}")
    print(f"credits: {account.credits}")
    return EXIT_OK


def cmd_search(args, config: Config) -> int:
    group = get_group(args.group)
    queries = [args.query] if args.query else list(group.queries)
    client = build_client(config, args.source)
    catalog = Catalog(config.catalog_path)
    limit = 50
    total = 0
    for query in queries:
        for page in range(1, args.pages + 1):
            result = client.search(query, group.category, page, limit=limit)
            catalog.record_search(result.samples, query, group.id, page, limit)
            total += len(result.samples)
            _log(f"{group.id}: {query!r} page {page}/{result.total_pages}: "
                 f"{len(result.samples)} samples ({result.total_records} total)")
            if page >= result.total_pages:
                break
    print(f"recorded {total} results; group {group.id} now has "
          f"{len(catalog.rows_for_group(group.id))} catalog rows")
    return EXIT_OK


# -- parser --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="splice-curator",
        description="Unofficial Splice sample curator. Spends credits only with `buy --confirm`.",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config TOML path")
    p.add_argument("--source", choices=("env", "chrome"), default="env",
                   help="token source: SPLICE_TOKEN env var or Chrome cookies")
    p.add_argument("--max-per-day", type=int, help="override max purchases per day")
    p.add_argument("--delay", action="append", metavar="KIND=MIN:MAX",
                   help="override a request delay range in seconds (repeatable)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source", choices=("env", "chrome"), default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("auth", parents=[common], help="show token expiry, user, and credits")
    s.set_defaults(func=cmd_auth)

    s = sub.add_parser("search", parents=[common], help="search one budget group into the catalog")
    s.add_argument("--group", required=True, help="group id: " + ", ".join(g.id for g in GROUPS))
    s.add_argument("--pages", type=int, default=4)
    s.add_argument("--query", help="one query instead of the group's queries")
    s.set_defaults(func=cmd_search)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_cli_config(args)
        return args.func(args, config)
    except (AuthError, AuthRejected) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "log in" not in str(exc):
            print("log in to splice.com again or check SPLICE_TOKEN", file=sys.stderr)
        return EXIT_AUTH
    except (SpliceError, KeyError, ValueError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {message}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
