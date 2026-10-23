"""Command-line entry point: ``splice-curator``."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

from .auth import COOKIE_NAMES, AuthError, load_session
from .budget import GROUPS, get_group
from .buy import CAP_REASONS, run_buy
from .catalog import Catalog
from .client import AuthRejected, SpliceClient, SpliceError
from .config import DEFAULT_CONFIG_PATH, DELAY_KINDS, Config, Delay, load_config
from .organize import run_organize
from .rank import build_shortlist
from .review import write_review_page
from .sync import refresh_owned, run_sync
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
    refresher = (lambda: load_session("chrome", time.time(), COOKIE_NAMES)) if source == "chrome" else None
    return SpliceClient(session, throttle, config, refresher=refresher)


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


def cmd_shortlist(args, config: Config) -> int:
    group = get_group(args.group)
    size = args.size or config.batch_size
    catalog = Catalog(config.catalog_path)
    samples = build_shortlist(catalog.rows_for_group(group.id), group, size, config.per_pack_cap)
    if not samples:
        print(f"nothing to shortlist for {group.id}; run `search --group {group.id}` first")
        return EXIT_OK
    batch_id = f"{group.id}-{datetime.now():%Y%m%d-%H%M%S}"
    path = write_review_page(samples, group, batch_id, config.data_dir / "review")
    catalog.mark_shortlisted([s.catalog_uuid for s in samples], batch_id)
    print(f"review page: {path}")
    print(f"{len(samples)} sounds; approving all would cost {len(samples)} credits "
          f"(group budget {group.credits})")
    return EXIT_OK


def cmd_sync(args, config: Config) -> int:
    client = build_client(config, args.source)
    catalog = Catalog(config.catalog_path)
    if not args.no_refresh:
        _log("listing owned sounds")
        print(f"{refresh_owned(client, catalog)} owned sounds listed by Splice")
    owned = len(catalog.licensed_rows())
    _log(f"{owned} owned sounds in the catalog; downloading missing ones to {config.staging_dir}")
    report = run_sync(
        client, catalog, config.staging_dir, [config.splice_dir, config.staging_dir],
        config.min_free_bytes, limit=args.limit,
    )
    print(f"downloaded {report.downloaded}, already on disk {report.skipped}, failed {len(report.failed)}")
    for uuid in report.failed:
        print(f"  failed: {uuid}")
    if report.stopped_reason:
        print(f"stopped: {report.stopped_reason}", file=sys.stderr)
        return EXIT_AUTH if "log in" in report.stopped_reason else EXIT_ERROR
    return EXIT_OK


def cmd_buy(args, config: Config) -> int:
    client = build_client(config, args.source)
    catalog = Catalog(config.catalog_path)
    caps = [c for c in (args.max_credits, config.max_purchases_per_run) if c is not None]
    max_credits = min(caps) if caps else None
    if args.confirm:
        _log(f"buying for real: up to {max_credits if max_credits is not None else 'all'} credits, "
             f"daily cap {config.max_purchases_per_day}")
    report = run_buy(
        client, catalog, Path(args.approved),
        confirm=args.confirm,
        max_credits=max_credits,
        daily_cap=config.max_purchases_per_day,
        today=date.today(),
        staging=None if args.no_download else config.staging_dir,
    )
    if report.dry_run:
        needed = report.planned - report.skipped_owned
        print(f"planned: {report.planned}")
        print(f"already owned: {report.skipped_owned}")
        print(f"credits needed: {needed}")
        print(f"credits available: {report.credits_before}")
        print(f"bought today: {catalog.purchases_on(date.today())} of {config.max_purchases_per_day}")
        print("dry run: no credits spent (add --confirm to buy)")
        return EXIT_OK
    print(f"bought {report.bought}, already owned {report.skipped_owned}, "
          f"credits {report.credits_before} -> {report.credits_after}")
    expected = None if report.credits_before is None else report.credits_before - report.bought
    if report.credits_after is not None and expected is not None and report.credits_after != expected:
        print(f"warning: expected {expected} credits left, Splice reports {report.credits_after}", file=sys.stderr)
    for uuid in report.download_failed:
        print(f"  download failed (run sync later): {uuid}")
    for uuid in report.uncertain:
        print(f"  UNCERTAIN, check on splice.com: {uuid}", file=sys.stderr)
    if report.stopped_reason:
        print(f"stopped: {report.stopped_reason}", file=sys.stderr)
        if report.stopped_reason in CAP_REASONS:
            return EXIT_OK
        return EXIT_AUTH if "log in" in report.stopped_reason else EXIT_ERROR
    return EXIT_OK


def cmd_organize(args, config: Config) -> int:
    catalog = Catalog(config.catalog_path)
    sources = [config.staging_dir, config.splice_dir]
    _log(f"organizing {', '.join(str(s) for s in sources)} into {args.target}")
    report = run_organize(catalog, sources, Path(args.target))
    print(f"copied {report.copied}, already there {report.skipped}, unsorted {report.unsorted}")
    print(f"catalog: {Path(args.target) / 'Samples' / 'catalog.csv'}")
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

    s = sub.add_parser("shortlist", help="rank a group and write an HTML review page")
    s.add_argument("--group", required=True)
    s.add_argument("--size", type=int, help="sounds per batch (default: config batch_size)")
    s.set_defaults(func=cmd_shortlist)

    s = sub.add_parser("sync", parents=[common], help="download owned sounds that are not on disk yet")
    s.add_argument("--limit", type=int, help="download at most N files")
    s.add_argument("--no-refresh", action="store_true", help="skip listing owned sounds from Splice")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("buy", parents=[common], help="license approved sounds (dry run unless --confirm)")
    s.add_argument("approved", metavar="APPROVED.json")
    s.add_argument("--confirm", action="store_true", help="really spend credits")
    s.add_argument("--max-credits", type=int, help="spend at most N credits this run")
    s.add_argument("--no-download", action="store_true", help="do not download after each purchase")
    s.set_defaults(func=cmd_buy)

    s = sub.add_parser("organize", help="copy local files into an organized library")
    s.add_argument("target", metavar="TARGET_DIR")
    s.set_defaults(func=cmd_organize)

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
    except (SpliceError, KeyError, ValueError, OSError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {message}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
