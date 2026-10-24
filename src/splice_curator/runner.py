"""Execute a saved autobuy plan with every ``buy_one`` guard (spec 5.4).

Dry run by default. With ``until_done`` the runner stops at the daily cap,
sleeps until local midnight, and goes on, so one command can cover several days.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from .buy import AUDIT_EVERY, MAX_PRICE_SKIPS, BuyReport, BuyRun, buy_lock, buy_one, resolve_uncertain
from .catalog import Catalog
from .client import SpliceClient, SpliceError
from .planfiles import PlanFile

PROGRESS_EVERY = 10
DAY_CAP = "daily cap reached"


@dataclass
class RunReport:
    buy: BuyReport
    skipped_rejected: int = 0
    days: list[tuple[date, int]] = field(default_factory=list)  # (day, bought that day)
    remaining: int = 0  # plan sounds not yet owned when the run ended


def seconds_until_midnight(now: datetime) -> float:
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    return (midnight - now).total_seconds() + 5


def _check_items(catalog: Catalog, items: list[dict]) -> dict[str, object]:
    rows = {i["catalog_uuid"]: catalog.get_by_catalog_uuid(i["catalog_uuid"]) for i in items}
    stale = [i for i in items if rows[i["catalog_uuid"]] is None
             or rows[i["catalog_uuid"]].shortlisted_batch != i["batch_id"]]
    if stale:
        raise ValueError(
            f"{len(stale)} plan sounds are not in their planned batches (released or re-planned?); "
            "run `autobuy plan` again"
        )
    return rows


def run_autobuy(
    client: SpliceClient,
    catalog: Catalog,
    plan: PlanFile,
    *,
    confirm: bool,
    max_credits: int | None,
    daily_cap: int,
    staging: Path | None,
    rejected: set[str] = frozenset(),
    until_done: bool = False,
    now: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    min_free_bytes: int = 0,
    max_price_skips: int = MAX_PRICE_SKIPS,
    audit_every: int = AUDIT_EVERY,
    disk_usage: Callable = shutil.disk_usage,
    log: Callable[[str], None] = lambda message: None,
) -> RunReport:
    if daily_cap < 1:
        raise ValueError("daily cap must be at least 1")
    items = [i for i in plan.items if i["catalog_uuid"] not in rejected]
    skipped_rejected = len(plan.items) - len(items)
    rows = _check_items(catalog, items)
    if confirm and staging is not None and not Path(staging).parent.is_dir():
        raise ValueError(f"staging folder's parent is missing: {Path(staging).parent} (is the drive connected?)")

    credits = client.account().credits
    if not confirm:
        report = BuyReport(len(items), 0, sum(1 for r in rows.values() if r.sample.licensed), [], None, True,
                           credits_before=credits)
        return RunReport(report, skipped_rejected, remaining=len(items) - report.skipped_owned)

    with buy_lock(catalog.path.parent):
        run = BuyRun(client, catalog, daily_cap=daily_cap, max_credits=max_credits, staging=staging,
                     credits=credits, now=now, max_price_skips=max_price_skips, audit_every=audit_every,
                     min_free_bytes=min_free_bytes, disk_usage=disk_usage)
        report = run.report
        report.planned = len(items)
        out = RunReport(report, skipped_rejected)
        report.stopped_reason = resolve_uncertain(client, catalog, now)
        if report.stopped_reason is None:
            rows = _check_items(catalog, items)  # licensed flags may have changed
            _loop(run, items, rows, out, until_done, now, sleep, log)
        out.remaining = sum(1 for i in items if not catalog.get_by_catalog_uuid(i["catalog_uuid"]).sample.licensed)
        try:
            report.credits_after = client.account().credits
        except SpliceError:
            report.credits_after = None
    return out


def _loop(run: BuyRun, items, rows, out: RunReport, until_done: bool, now, sleep, log) -> None:
    report = run.report
    started, last_logged = now(), 0
    day_start, day_bought = now().date(), report.bought
    index = 0
    while index < len(items):
        item = items[index]
        reason = buy_one(run, rows[item["catalog_uuid"]], item["price"])
        if reason == DAY_CAP and until_done:
            out.days.append((day_start, report.bought - day_bought))
            log(f"{day_start}: bought {report.bought - day_bought}; credits left {run.credits}; "
                f"{len(items) - index} sounds to go; waiting for the next day")
            sleep(seconds_until_midnight(now()))
            day_start, day_bought = now().date(), report.bought
            continue  # same item again on the new day
        if reason:
            report.stopped_reason = reason
            break
        index += 1
        if report.bought and report.bought != last_logged and report.bought % PROGRESS_EVERY == 0:
            last_logged = report.bought
            elapsed = (now() - started).total_seconds()
            eta = elapsed / report.bought * (len(items) - index)
            log(f"bought {report.bought} ({report.credits_spent} credits), {len(items) - index} to go, "
                f"credits left {run.credits}, about {eta / 60:.0f} min left")
    out.days.append((day_start, report.bought - day_bought))
