"""License approved sounds, with guards against spending credits by mistake.

Dry run unless ``confirm``. A purchase is never retried: any doubt about
whether one went through stops the run, and the next run re-checks
``licensed`` before buying, so nothing is bought twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .catalog import Catalog
from .client import AuthRejected, PurchaseUncertain, RateLimited, SpliceClient, SpliceError
from .review import load_approved
from .sync import download_one

CAP_REASONS = ("daily cap reached", "max credits for this run reached")


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

    report = BuyReport(
        planned=len(uuids), bought=0, skipped_owned=0, uncertain=[],
        stopped_reason=None, dry_run=not confirm,
    )
    credits = client.account().credits
    report.credits_before = credits

    if not confirm:
        report.skipped_owned = sum(1 for r in rows.values() if r.sample.licensed)
        return report

    for uuid in uuids:
        if catalog.purchases_on(today) >= daily_cap:
            report.stopped_reason = "daily cap reached"
            break
        if max_credits is not None and report.bought >= max_credits:
            report.stopped_reason = "max credits for this run reached"
            break
        try:
            files = client.asset_files(uuid)
        except (AuthRejected, RateLimited) as exc:
            report.stopped_reason = str(exc)
            break
        except SpliceError as exc:
            report.stopped_reason = f"license check failed for {uuid}: {exc}"
            break
        if files.licensed:
            catalog.mark_licensed(uuid)
            catalog.log_purchase(uuid, "skipped_owned", credits, credits, now())
            report.skipped_owned += 1
            continue
        if credits is None:
            report.stopped_reason = "credit balance unknown"
            break
        if credits < 1:
            report.stopped_reason = "out of credits"
            break

        try:
            ok = client.purchase(uuid)
        except PurchaseUncertain as exc:
            catalog.log_purchase(uuid, "uncertain", credits, None, now())
            report.uncertain.append(uuid)
            report.stopped_reason = f"purchase result uncertain for {uuid} ({exc}); check Splice before re-running"
            break
        except AuthRejected as exc:
            report.stopped_reason = str(exc)
            break
        if not ok:
            catalog.log_purchase(uuid, "failed", credits, credits, now())
            report.stopped_reason = f"Splice did not license {uuid}; stopping"
            break

        catalog.mark_licensed(uuid)
        catalog.log_purchase(uuid, "bought", credits, credits - 1, now())
        credits -= 1
        report.bought += 1

        if staging is not None:
            try:
                download_one(client, catalog, rows[uuid], Path(staging))
            except AuthRejected as exc:
                report.stopped_reason = str(exc)
                break
            except (SpliceError, OSError):
                report.download_failed.append(uuid)  # `sync` will fetch it later

    try:
        report.credits_after = client.account().credits
    except SpliceError:
        report.credits_after = None
    return report
