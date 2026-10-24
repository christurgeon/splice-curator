"""License approved sounds, with guards against spending credits by mistake.

Dry run unless ``confirm``. A purchase is never retried: any doubt about
whether one went through stops the run, and the next run re-checks
``licensed`` before buying, so nothing is bought twice. ``buy_one`` holds
every per-sound guard and is shared by ``run_buy`` and the autobuy runner.
"""

from __future__ import annotations

import fcntl
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .catalog import Catalog, CatalogRow
from .client import AuthRejected, PurchaseUncertain, RateLimited, SpliceClient, SpliceError
from .review import load_approved
from .sync import download_one

KEEP_REACHED = "credits down to keep"
CAP_REASONS = ("daily cap reached", "max credits for this run reached", KEEP_REACHED)
LOCK_NAME = "buy.lock"
AUDIT_EVERY = 25
MAX_PRICE_SKIPS = 5


@dataclass
class BuyReport:
    planned: int
    bought: int
    skipped_owned: int
    uncertain: list[str]
    stopped_reason: str | None
    dry_run: bool
    credits_before: int | None = None
    credits_after: int | None = None
    download_failed: list[str] = field(default_factory=list)
    credits_spent: int = 0
    price_skips: int = 0


@contextmanager
def buy_lock(data_dir: Path):
    """Exclusive lock so two buying processes never run at once."""
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(data_dir) / LOCK_NAME, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another buy is running (buy.lock is held); wait for it to end") from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@dataclass
class BuyRun:
    """State and limits shared by every ``buy_one`` call of one run."""

    client: SpliceClient
    catalog: Catalog
    daily_cap: int
    max_credits: int | None
    staging: Path | None
    credits: int | None
    now: Callable[[], datetime] = datetime.now
    max_price_skips: int = MAX_PRICE_SKIPS
    audit_every: int = AUDIT_EVERY
    min_free_bytes: int = 0
    disk_usage: Callable = shutil.disk_usage
    keep: int = 0  # never let the balance fall below this (autobuy passes config.keep)
    report: BuyReport = field(default_factory=lambda: BuyReport(0, 0, 0, [], None, False))
    _since_audit: int = 0

    def __post_init__(self) -> None:
        self.report.credits_before = self.credits


def resolve_uncertain(client: SpliceClient, catalog: Catalog, now: Callable[[], datetime]) -> str | None:
    """Settle earlier `uncertain` purchases with a read-only license check. Returns a stop reason."""
    for uuid in catalog.unresolved_uncertain():
        try:
            files = client.asset_files(uuid)
        except (AuthRejected, RateLimited) as exc:
            return str(exc)
        except SpliceError as exc:
            return f"cannot resolve uncertain purchase {uuid}: {exc}"
        if files.licensed:
            catalog.mark_licensed(uuid)
            catalog.log_purchase(uuid, "bought", None, None, now())
        else:
            catalog.log_purchase(uuid, "not_bought", None, None, now())
    return None


def _skip(run: BuyRun, uuid: str, result: str, when: datetime) -> str | None:
    """Log a sound that was not bought and go on, or stop after ``max_price_skips`` skips."""
    run.catalog.log_purchase(uuid, result, run.credits, run.credits, when)
    run.report.price_skips += 1
    if run.report.price_skips >= run.max_price_skips:
        return f"stopped after {run.report.price_skips} skipped sounds (last: {result} for {uuid})"
    return None


def audit(run: BuyRun) -> str | None:
    try:
        actual = run.client.account().credits
    except AuthRejected as exc:
        return str(exc)
    except SpliceError as exc:
        return f"balance audit failed: {exc}"
    if actual != run.credits:
        return f"balance audit mismatch: expected {run.credits} credits, Splice reports {actual}"
    return None


def buy_one(run: BuyRun, row: CatalogRow, expected_price: int = 1) -> str | None:
    """Buy one sound. Returns a stop reason, or None to continue."""
    sample, report, catalog, client = row.sample, run.report, run.catalog, run.client
    uuid = sample.catalog_uuid
    if sample.licensed:  # known owned: no request
        report.skipped_owned += 1
        return None
    when = run.now()
    if catalog.purchases_on(when.date()) >= run.daily_cap:
        return "daily cap reached"
    if run.max_credits is not None and report.credits_spent + expected_price > run.max_credits:
        return "max credits for this run reached"
    if run.keep and run.credits is not None and run.credits - expected_price < run.keep:
        return KEEP_REACHED
    if run.staging is not None:
        staging = Path(run.staging)
        staging.mkdir(exist_ok=True)
        if run.disk_usage(staging).free < run.min_free_bytes:
            return "low disk space"
    try:
        files = client.asset_files(uuid)
    except (AuthRejected, RateLimited) as exc:
        return str(exc)
    except SpliceError:
        # Nothing was bought (for example a sound removed from Splice): skip it, do not stop the run.
        return _skip(run, uuid, "check_failed", when)
    credits = run.credits
    if files.price is not None:
        catalog.set_price(uuid, files.price)
    if files.licensed:
        catalog.mark_licensed(uuid)
        catalog.log_purchase(uuid, "skipped_owned", credits, credits, when)
        report.skipped_owned += 1
        return None
    if files.price != expected_price:
        return _skip(run, uuid, "price_unknown" if files.price is None else "price_mismatch", when)
    if credits is None:
        return "credit balance unknown"
    if credits < expected_price:
        return "out of credits"

    attempt = catalog.begin_purchase(uuid, credits, when)  # stays `uncertain` if this process dies
    try:
        ok = client.purchase(uuid)
    except PurchaseUncertain as exc:
        report.uncertain.append(uuid)
        return f"purchase result uncertain for {uuid} ({exc}); check Splice before re-running"
    except AuthRejected as exc:
        return str(exc)  # the row stays `uncertain`; the next run checks it read-only
    done = run.now()  # the day of the purchase itself counts toward the daily cap
    if not ok:
        catalog.finish_purchase(attempt, "failed", credits, done)
        return f"Splice did not license {uuid}; stopping"

    catalog.finish_purchase(attempt, "bought", credits - expected_price, done)
    run.credits = credits - expected_price
    report.bought += 1
    report.credits_spent += expected_price
    run._since_audit += 1

    if run.staging is not None:
        try:
            download_one(client, catalog, row, Path(run.staging))
        except AuthRejected as exc:
            return str(exc)
        except (SpliceError, OSError):
            report.download_failed.append(uuid)  # `sync` will fetch it later
    if run._since_audit >= run.audit_every:
        run._since_audit = 0
        return audit(run)
    return None


def run_buy(
    client: SpliceClient,
    catalog: Catalog,
    approved_path: Path,
    *,
    confirm: bool,
    max_credits: int | None,
    daily_cap: int,
    today: date,
    staging: Path | None,
    now: Callable[[], datetime] = datetime.now,
    min_free_bytes: int = 0,
    max_price_skips: int = MAX_PRICE_SKIPS,
    audit_every: int = AUDIT_EVERY,
    disk_usage: Callable = shutil.disk_usage,
) -> BuyReport:
    batch_id, _group_id, uuids = load_approved(approved_path)
    rows = {u: catalog.get_by_catalog_uuid(u) for u in uuids}
    foreign = [u for u, r in rows.items() if r is None or r.shortlisted_batch != batch_id]
    if foreign:
        raise ValueError(
            f"{len(foreign)} approved sounds are not in shortlist batch {batch_id}; "
            "export the approved file from that batch's review page"
        )
    if confirm and staging is not None and not Path(staging).parent.is_dir():
        raise ValueError(f"staging folder's parent is missing: {Path(staging).parent} (is the drive connected?)")

    credits = client.account().credits
    if not confirm:
        report = BuyReport(len(uuids), 0, sum(1 for r in rows.values() if r.sample.licensed), [],
                           None, True, credits_before=credits)
        return report

    with buy_lock(catalog.path.parent):
        run = BuyRun(client, catalog, daily_cap=daily_cap, max_credits=max_credits, staging=staging,
                     credits=credits, now=now, max_price_skips=max_price_skips, audit_every=audit_every,
                     min_free_bytes=min_free_bytes, disk_usage=disk_usage)
        run.report.planned = len(uuids)
        report = run.report
        report.stopped_reason = resolve_uncertain(client, catalog, now)
        if report.stopped_reason is None:
            rows = {u: catalog.get_by_catalog_uuid(u) for u in uuids}  # licensed flags may have changed
            for uuid in uuids:
                report.stopped_reason = buy_one(run, rows[uuid])
                if report.stopped_reason:
                    break
        try:
            report.credits_after = client.account().credits
        except SpliceError:
            report.credits_after = None
    return report
