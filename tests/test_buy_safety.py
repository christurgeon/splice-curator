"""Money-safety guards (autobuy task 0)."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from splice_curator.buy import BuyRun, buy_lock, buy_one
from tests.test_buy import NOW, buy, purchased, setup


def make_run(fake_client, catalog, **kw):
    args = dict(daily_cap=400, max_credits=None, staging=None, now=NOW, credits=fake_client.credits)
    args.update(kw)
    return BuyRun(fake_client, catalog, **args)


def test_price_above_expected_is_skipped_and_logged(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    fake_client.prices[samples[0].catalog_uuid] = 3
    report = buy(fake_client, catalog, path)
    assert purchased(fake_client) == [samples[1].catalog_uuid, samples[2].catalog_uuid]
    assert report.bought == 2 and report.credits_spent == 2
    results = [tuple(r) for r in catalog.db.execute("SELECT catalog_uuid, result FROM purchases ORDER BY id")]
    assert (samples[0].catalog_uuid, "price_mismatch") in results


def test_unknown_price_is_skipped(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=2)
    fake_client.prices[samples[0].catalog_uuid] = None
    report = buy(fake_client, catalog, path)
    assert purchased(fake_client) == [samples[1].catalog_uuid]
    row = catalog.db.execute(
        "SELECT result FROM purchases WHERE catalog_uuid = ?", (samples[0].catalog_uuid,)).fetchone()
    assert row["result"] == "price_unknown"
    assert report.stopped_reason is None


def test_stops_after_max_price_skips(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=6)
    for s in samples[:3]:
        fake_client.prices[s.catalog_uuid] = None
    report = buy(fake_client, catalog, path, max_price_skips=3)
    assert report.bought == 0
    assert "price" in report.stopped_reason
    assert fake_client.count("purchase") == 0


def _count_rows(catalog, uuid, result):
    return catalog.db.execute(
        "SELECT COUNT(*) FROM purchases WHERE catalog_uuid = ? AND result = ?", (uuid, result)).fetchone()[0]


def test_repeat_skips_do_not_count(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=6)
    for s in samples[:3]:
        fake_client.prices[s.catalog_uuid] = None
        catalog.log_purchase(s.catalog_uuid, "price_unknown", None, None, datetime(2026, 10, 23))
    report = buy(fake_client, catalog, path, max_price_skips=3)
    assert report.stopped_reason is None
    assert report.price_skips == 0
    assert purchased(fake_client) == [s.catalog_uuid for s in samples[3:]]
    for s in samples[:3]:
        assert _count_rows(catalog, s.catalog_uuid, "price_unknown") == 2


def test_new_skips_still_stop_the_run(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=8)
    for s in samples[:2]:
        catalog.log_purchase(s.catalog_uuid, "price_unknown", None, None, datetime(2026, 10, 23))
    for s in samples[:5]:
        fake_client.prices[s.catalog_uuid] = None
    report = buy(fake_client, catalog, path, max_price_skips=3)
    assert report.price_skips == 3
    assert "price" in report.stopped_reason


def test_different_result_counts_as_new(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=4)
    for s in samples[:3]:
        catalog.log_purchase(s.catalog_uuid, "price_mismatch", None, None, datetime(2026, 10, 23))
        fake_client.prices[s.catalog_uuid] = None
    report = buy(fake_client, catalog, path, max_price_skips=3)
    assert report.price_skips == 3
    assert "price" in report.stopped_reason


def test_expected_price_other_than_one(tmp_path, fake_client):
    samples, catalog, _ = setup(tmp_path, fake_client, n=1)
    fake_client.prices[samples[0].catalog_uuid] = 3
    run = make_run(fake_client, catalog)
    assert buy_one(run, catalog.get_by_catalog_uuid(samples[0].catalog_uuid), 3) is None
    assert run.report.credits_spent == 3 and run.credits == 3357
    assert fake_client.credits == 3357


def test_max_credits_counts_credits(tmp_path, fake_client):
    samples, catalog, _ = setup(tmp_path, fake_client, n=3)
    for s in samples:
        fake_client.prices[s.catalog_uuid] = 3
    run = make_run(fake_client, catalog, max_credits=5)
    assert buy_one(run, catalog.get_by_catalog_uuid(samples[0].catalog_uuid), 3) is None
    reason = buy_one(run, catalog.get_by_catalog_uuid(samples[1].catalog_uuid), 3)
    assert reason == "max credits for this run reached"
    assert run.report.credits_spent == 3


def test_not_enough_credits_for_price(tmp_path, fake_client):
    samples, catalog, _ = setup(tmp_path, fake_client, n=1)
    fake_client.prices[samples[0].catalog_uuid] = 3
    fake_client.credits = 2
    run = make_run(fake_client, catalog)
    assert buy_one(run, catalog.get_by_catalog_uuid(samples[0].catalog_uuid), 3) == "out of credits"
    assert fake_client.count("purchase") == 0


def test_balance_audit_mismatch_stops(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=6)
    fake_client.credit_drift = 1  # server takes 2 credits per sound
    report = buy(fake_client, catalog, path, audit_every=2)
    assert report.bought == 2
    assert "balance" in report.stopped_reason
    assert fake_client.count("purchase") == 2


def test_balance_audit_ok_continues(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=5)
    report = buy(fake_client, catalog, path, audit_every=2)
    assert report.bought == 5 and report.stopped_reason is None


def test_daily_cap_uses_clock_before_each_purchase(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=4)
    # first purchase: checked and completed at 23:59; the rest after midnight
    clock = iter([datetime(2026, 10, 24, 23, 59)] * 2 + [datetime(2026, 10, 25, 0, 1)] * 50)
    report = buy(fake_client, catalog, path, daily_cap=1, now=lambda: next(clock))
    assert report.stopped_reason == "daily cap reached"
    assert report.bought == 2
    days = [r["day"] for r in catalog.db.execute("SELECT day FROM purchases WHERE result = 'bought' ORDER BY id")]
    assert days == ["2026-10-24", "2026-10-25"]


def test_lock_held_blocks_second_run(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    with buy_lock(catalog.path.parent):
        with pytest.raises(ValueError, match="another buy"):
            buy(fake_client, catalog, path)
    assert fake_client.count("purchase") == 0
    assert buy(fake_client, catalog, path).bought == 3  # lock released


def test_resume_skips_licensed_rows_without_api_calls(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    for s in samples:
        catalog.mark_licensed(s.catalog_uuid)
    report = buy(fake_client, catalog, path)
    assert report.skipped_owned == 3 and report.bought == 0
    assert fake_client.count("asset_files") == 0 and fake_client.count("purchase") == 0


def test_uncertain_resolved_when_licensed(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    catalog.log_purchase(samples[0].catalog_uuid, "uncertain", 3360, None, NOW())
    fake_client.owned.add(samples[0].catalog_uuid)
    report = buy(fake_client, catalog, path)
    assert purchased(fake_client) == [samples[1].catalog_uuid, samples[2].catalog_uuid]
    assert catalog.get_by_catalog_uuid(samples[0].catalog_uuid).sample.licensed
    assert catalog.unresolved_uncertain() == []
    assert report.uncertain == []


def test_uncertain_not_licensed_is_left_for_the_plan(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=1)
    catalog.log_purchase(samples[0].catalog_uuid, "uncertain", 3360, None, NOW())
    report = buy(fake_client, catalog, path)
    assert report.bought == 1  # not licensed on Splice, so the plan buys it
    assert catalog.unresolved_uncertain() == []


def test_low_free_space_stops_before_buying(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    report = buy(fake_client, catalog, path, staging=tmp_path / "staging", min_free_bytes=10,
                 disk_usage=lambda p: SimpleNamespace(free=5))
    assert report.stopped_reason == "low disk space"
    assert fake_client.count("purchase") == 0
