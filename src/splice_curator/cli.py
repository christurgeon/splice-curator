"""Command-line entry point: ``splice-curator``."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

from .auth import COOKIE_NAMES, AuthError, load_session
from .budget import GROUPS, Genre, genre_quotas, get_genre, get_group
from .buy import CAP_REASONS, run_buy
from .catalog import Catalog
from .discover import run_discover
from .planfiles import estimate_bytes_per_sound, save_plan, summarize
from .quality import build_context
from .review import load_rejected
from .selection import AUTOBUY_SUFFIX, make_plan
from .client import AuthRejected, SpliceClient, SpliceError
from .config import DEFAULT_CONFIG_PATH, DELAY_KINDS, Config, Delay, load_config
from .organize import run_organize
from .rank import build_genre_shortlist, build_shortlist
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


def _pick_genres(args, config: Config) -> list[Genre] | None:
    """The enabled genres to use, or None (after printing an error) for a bad ``--genre``."""
    if not args.genre:
        return list(config.genres)
    try:
        return [get_genre(args.genre, config.genres)]
    except KeyError as exc:
        print(f"error: {exc.args[0]} (a genre with weight 0 is disabled)", file=sys.stderr)
        return None


def _search_plan(args, group, genres: list[Genre]) -> list[tuple[str, str | None, str | None]]:
    """``(query text, genre id, subtype)`` for every query to run."""
    if args.query:
        return [(args.query, None, None)]
    if not group.subtypes:
        return [(q, None, None) for q in group.queries]
    return [
        (f"{prefix} {subtype}", genre.id, subtype)
        for genre in genres
        for subtype in group.subtypes
        for prefix in genre.prefixes
    ]


def cmd_search(args, config: Config) -> int:
    group = get_group(args.group)
    genres = [] if args.query else _pick_genres(args, config)
    if genres is None:
        return EXIT_AUTH
    plan = _search_plan(args, group, genres)
    client = build_client(config, args.source)
    catalog = Catalog(config.catalog_path)
    limit = 50
    total = 0
    for query, genre_id, subtype in plan:
        for page in range(1, args.pages + 1):
            result = client.search(query, group.category, page, limit=limit)
            catalog.record_search(result.samples, query, group.id, page, limit,
                                  genre=genre_id, subtype=subtype)
            total += len(result.samples)
            _log(f"{group.id}: {query!r} page {page}/{result.total_pages}: "
                 f"{len(result.samples)} samples ({result.total_records} total)")
            if page >= result.total_pages:
                break
    print(f"recorded {total} results; group {group.id} now has "
          f"{len(catalog.rows_for_group(group.id))} catalog rows")
    return EXIT_OK


def cmd_discover(args, config: Config) -> int:
    genres = _pick_genres(args, config)
    if genres is None:
        return EXIT_AUTH
    client = build_client(config, args.source)
    catalog = Catalog(config.catalog_path)
    report = run_discover(client, catalog, config, genres, force=args.force, log=_log)
    for genre_id, n in report.packs_per_genre.items():
        print(f"{genre_id}: {n} packs")
    print(f"packs fetched {report.packs_fetched}, cached {report.packs_cached}, "
          f"sounds recorded {report.samples_recorded}")
    print("requests: " + ", ".join(f"{k} {v}" for k, v in sorted(report.requests.items())))
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if report.stopped_reason:
        print(f"stopped: {report.stopped_reason}", file=sys.stderr)
        return EXIT_AUTH if "log in" in report.stopped_reason else EXIT_ERROR
    return EXIT_OK


def cmd_shortlist(args, config: Config) -> int:
    group = get_group(args.group)
    catalog = Catalog(config.catalog_path)
    genre = None
    if args.genre:
        picked = _pick_genres(args, config)
        if picked is None:
            return EXIT_AUTH
        genre = picked[0]
        left = genre_quotas(group, config.genres)[genre.id] - catalog.count_spent(group.id, genre.id)
        if left <= 0:
            print(f"quota for {group.id}/{genre.id} is used up")
            return EXIT_OK
        size = args.size or min(left, config.batch_size)
        samples = build_genre_shortlist(
            catalog.rows_for_group(group.id, genre.id), group, genre, size, config.per_pack_cap)
        batch_id = f"{group.id}-{genre.id}-{datetime.now():%Y%m%d-%H%M%S}"
    else:
        size = args.size or config.batch_size
        samples = build_shortlist(catalog.rows_for_group(group.id), group, size, config.per_pack_cap)
        batch_id = f"{group.id}-{datetime.now():%Y%m%d-%H%M%S}"
    if not samples:
        print(f"nothing to shortlist for {group.id}; run `search --group {group.id}` first")
        return EXIT_OK
    path = write_review_page(samples, group, batch_id, config.data_dir / "review",
                             genre_title=genre.title if genre else None)
    catalog.mark_shortlisted([s.catalog_uuid for s in samples], batch_id)
    print(f"review page: {path}")
    print(f"{len(samples)} sounds; approving all would cost {len(samples)} credits "
          f"(group budget {group.credits})")
    return EXIT_OK


def cmd_quota(args, config: Config) -> int:
    group = get_group(args.group)
    catalog = Catalog(config.catalog_path)
    quotas = genre_quotas(group, config.genres)
    header = ("genre", "quota", "searched", "shortlisted", "bought", "remaining")
    table = [header]
    for genre in config.genres:
        rows = catalog.rows_for_group(group.id, genre.id)
        picked = [r for r in rows if (r.shortlisted_batch or "").startswith(f"{group.id}-{genre.id}-")]
        bought = sum(1 for r in picked if r.sample.licensed)
        shortlisted = len(picked) - bought
        remaining = max(0, quotas[genre.id] - catalog.count_spent(group.id, genre.id))
        table.append((genre.id, *map(str, (quotas[genre.id], len(rows), shortlisted, bought, remaining))))
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for row in table:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
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
        min_free_bytes=config.min_free_bytes,
        max_price_skips=config.max_price_skips,
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
    print(f"bought {report.bought} ({report.credits_spent} credits), already owned {report.skipped_owned}, "
          f"credits {report.credits_before} -> {report.credits_after}")
    expected = None if report.credits_before is None else report.credits_before - report.credits_spent
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


def _free_bytes(path: Path) -> int | None:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def cmd_autobuy_plan(args, config: Config) -> int:
    if args.group != "all":
        get_group(args.group)  # KeyError -> message
    only = None if args.group == "all" else frozenset({args.group})
    client = build_client(config, args.source)
    credits = client.account().credits
    if credits is None:
        print("error: Splice did not report a credit balance", file=sys.stderr)
        return EXIT_ERROR
    catalog = Catalog(config.catalog_path)
    if args.replace:
        if args.confirm:
            print(f"released {catalog.release_pending(suffix=AUTOBUY_SUFFIX)} sounds from older autobuy plans")
        else:
            catalog = catalog.in_memory_copy()
            released = catalog.release_pending(suffix=AUTOBUY_SUFFIX)
            print(f"dry run: would release {released} sounds from older autobuy plans")
    ctx = build_context(catalog, config)
    plan = make_plan(catalog, config, ctx, credits, datetime.now(), only)
    print(summarize(plan, ctx, config, date.today(), _free_bytes(config.staging_dir),
                    estimate_bytes_per_sound(config.staging_dir)))
    if not args.confirm:
        print("\ndry run: nothing saved (add --confirm to save this plan; planning never spends credits)")
        return EXIT_OK
    saved = save_plan(catalog, plan, config, with_page=args.page)
    print(f"\nplan saved: {saved['plan']}")
    print(f"csv: {saved['csv']}")
    if "page" in saved:
        print(f"spot-check page: {saved['page']}")
    print(f"run it with: splice-curator autobuy run {saved['plan']}   (dry run unless --confirm)")
    return EXIT_OK


def cmd_release(args, config: Config) -> int:
    catalog = Catalog(config.catalog_path)
    if args.rejected:
        uuids = load_rejected(Path(args.rejected))
        tag = f"rejected-{Path(args.rejected).stem}"[:100]
        print(f"rejected {catalog.reject(uuids, tag)} sounds; they leave their batches and are never planned again")
        return EXIT_OK
    if args.batch:
        print(f"released {catalog.release_pending(batch_id=args.batch)} sounds from batch {args.batch}")
    else:
        print(f"released {catalog.release_pending(suffix=AUTOBUY_SUFFIX)} sounds from autobuy plans")
    for batch, n in catalog.pending_batches().items():
        print(f"  still pending: {batch} ({n})")
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
    s.add_argument("--genre", help="search one genre only (default: every enabled genre)")
    s.add_argument("--pages", type=int, default=2)
    s.add_argument("--query", help="one query instead of the genre and subtype queries (no genre)")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("discover", parents=[common],
                       help="find the best packs and their best sounds per genre (free, read-only)")
    s.add_argument("--genre", help="discover one genre only (default: every enabled genre)")
    s.add_argument("--force", action="store_true", help="ignore cached charts and packs")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("shortlist", help="rank a group and write an HTML review page")
    s.add_argument("--group", required=True)
    s.add_argument("--genre", help="shortlist one genre (default size: its remaining quota)")
    s.add_argument("--size", type=int, help="sounds per batch (default: config batch_size)")
    s.set_defaults(func=cmd_shortlist)

    s = sub.add_parser("quota", help="show genre quotas and progress for a group (offline)")
    s.add_argument("--group", required=True)
    s.set_defaults(func=cmd_quota)

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

    s = sub.add_parser("autobuy", help="plan and run automatic buying")
    auto = s.add_subparsers(dest="autobuy_command", required=True)
    a = auto.add_parser("plan", parents=[common],
                        help="choose sounds for the live balance minus keep (free; saves only with --confirm)")
    a.add_argument("--group", default="all", help="a group id, or all (default)")
    a.add_argument("--replace", action="store_true", help="first release unbought sounds of older autobuy plans")
    a.add_argument("--page", action="store_true", help="also write a spot-check page (with --confirm)")
    a.add_argument("--confirm", action="store_true", help="save the plan and mark its batches (spends nothing)")
    a.set_defaults(func=cmd_autobuy_plan)

    s = sub.add_parser("release", help="return unbought planned sounds to the quotas (offline)")
    s.add_argument("batch", nargs="?", help="one batch id (default: all autobuy plans)")
    s.add_argument("--rejected", metavar="REJECTED.json", help="drop the sounds you rejected on the plan page")
    s.set_defaults(func=cmd_release)

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
