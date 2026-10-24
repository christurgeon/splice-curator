"""Adversarial review (autobuy Task 7): each test shows a problem that was found and fixed."""

from datetime import datetime, timedelta

from splice_curator.budget import GROUPS
from splice_curator.buy import KEEP_REACHED
from splice_curator.catalog import Catalog
from splice_curator.client import SpliceError
from splice_curator.sync import staging_path
from tests.test_buy import TODAY, buy, purchased
from tests.test_buy import setup as buy_setup
from tests.test_runner import Clock, bought, go
from tests.test_runner import setup as run_setup
from tests.test_selection import World


def _bought_in_batch(w: World, group: str, genre: str, n: int) -> None:
    start = w.n
    w.add(group, genre=genre, pack=900, n=n)
    rows = [r for r in w.cat.all_rows() if int(r.sample.file_hash, 16) > start]
    w.cat.mark_shortlisted([r.sample.catalog_uuid for r in rows], f"{group}-{genre}-20261001-100000")
    for r in rows:
        w.cat.mark_licensed(r.sample.catalog_uuid)


def _many_candidates(w: World) -> None:
    for g in GROUPS:
        for genre in ("tech_house", "minimal", "deep_house", "afro_house", "classic_house"):
            for p in range(1, 4):
                w.add(g.id, genre=genre, pack=p + 10 * GROUPS.index(g), n=4)


# -- planning more than live balance - keep -----------------------------------------


def test_plan_never_exceeds_spendable_when_bought_sounds_overfill_a_cell(tmp_path):
    w = World(tmp_path, keep=0)
    _bought_in_batch(w, "kicks", "tech_house", 500)  # far over that cell's quota
    _many_candidates(w)
    plan = w.plan(balance=100)
    assert plan.total <= plan.spendable == 100


def test_plan_subtracts_pending_batches_without_a_genre(tmp_path):
    w = World(tmp_path, keep=0)
    _many_candidates(w)
    manual = [r.sample.catalog_uuid for r in w.cat.rows_for_group("kicks", "tech_house")][:10]
    w.cat.mark_shortlisted(manual, "kicks-20261024-120000")  # old `shortlist --group kicks` batch, unbought
    plan = w.plan(balance=100)
    assert plan.pending == 10
    assert plan.total + plan.pending <= 100


# -- runner -------------------------------------------------------------------------


def test_runner_skips_sounds_released_during_the_run(tmp_path, fake_client):
    samples, catalog, plan = run_setup(tmp_path, fake_client)
    real = fake_client.purchase

    def purchase_then_replan(uuid):  # `autobuy plan --replace --confirm` runs while the runner sleeps
        ok = real(uuid)
        Catalog(catalog.path).release_pending(suffix="-ab")
        return ok

    fake_client.purchase = purchase_then_replan
    result = go(fake_client, catalog, plan)
    assert result.buy.bought == 1 and result.skipped_stale == 4
    assert len(bought(fake_client)) == 1


def test_runner_never_spends_below_keep(tmp_path, fake_client):
    _, catalog, plan = run_setup(tmp_path, fake_client)
    fake_client.credits = 203
    result = go(fake_client, catalog, plan, keep=200)
    assert result.buy.bought == 3 and result.buy.stopped_reason == KEEP_REACHED
    assert fake_client.credits == 200


def test_until_done_audits_the_balance_after_the_overnight_wait(tmp_path, fake_client):
    _, catalog, plan = run_setup(tmp_path, fake_client)
    clock = Clock()
    real_sleep = clock.sleep

    def sleep_and_buy_elsewhere(seconds):
        real_sleep(seconds)
        fake_client.credits -= 10  # the owner buys on splice.com overnight

    result = go(fake_client, catalog, plan, clock=clock, daily_cap=2, until_done=True, sleep=sleep_and_buy_elsewhere)
    assert result.buy.bought == 2
    assert "balance audit mismatch" in result.buy.stopped_reason


def test_removed_sound_is_skipped_and_the_run_goes_on(tmp_path, fake_client):
    samples, catalog, plan = run_setup(tmp_path, fake_client)
    first = plan.items[0]["catalog_uuid"]
    fake_client.errors[("asset_files", first)] = SpliceError(f"asset {first} not found")
    result = go(fake_client, catalog, plan)
    assert result.buy.bought == 4 and result.buy.stopped_reason is None
    row = catalog.db.execute("SELECT result FROM purchases WHERE catalog_uuid = ?", (first,)).fetchone()
    assert row["result"] == "check_failed"


# -- purchase records and the daily cap -------------------------------------------------


def test_crash_after_purchase_keeps_a_record_that_counts_toward_the_cap(tmp_path, fake_client):
    samples, catalog, path = buy_setup(tmp_path, fake_client)
    real = fake_client.purchase

    def purchase_then_crash(uuid):
        real(uuid)
        raise KeyboardInterrupt  # Ctrl-C after Splice licensed the sound, before the catalog write

    fake_client.purchase = purchase_then_crash
    try:
        buy(fake_client, catalog, path, daily_cap=1)
    except KeyboardInterrupt:
        pass
    fake_client.purchase = real
    report = buy(fake_client, catalog, path, daily_cap=1)
    assert catalog.purchases_on(TODAY) == 1
    assert report.stopped_reason == "daily cap reached"
    assert purchased(fake_client) == [samples[0].catalog_uuid]


def test_purchase_finished_after_midnight_counts_on_the_new_day(tmp_path, fake_client):
    samples, catalog, path = buy_setup(tmp_path, fake_client)
    times = [datetime(2026, 10, 24, 23, 59, 59), datetime(2026, 10, 25, 0, 0, 1), datetime(2026, 10, 25, 0, 0, 30)]
    clock = iter(times + [times[-1]] * 20)
    report = buy(fake_client, catalog, path, daily_cap=1, now=lambda: next(clock))
    assert report.bought == 1 and report.stopped_reason == "daily cap reached"
    assert catalog.purchases_on(TODAY + timedelta(days=1)) == 1


def test_catalog_waits_for_a_concurrent_writer(tmp_path):
    catalog = Catalog(tmp_path / "c.sqlite")
    (ms,) = catalog.db.execute("PRAGMA busy_timeout").fetchone()
    assert ms >= 30_000  # `discover` and `autobuy run` share the file


# -- files ------------------------------------------------------------------------------


def test_staging_path_stays_inside_staging_for_odd_names(tmp_path):
    staging = tmp_path / "staging"
    for name in ("", "/", "../..", "./.", "a/../../b.wav"):
        dest = staging_path(staging, name)
        assert dest != staging and staging in dest.parents
