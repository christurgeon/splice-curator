import json
from datetime import date, datetime

import pytest

from splice_curator.buy import run_buy
from splice_curator.catalog import Catalog
from splice_curator.client import AuthRejected, PurchaseUncertain
from tests.helpers import make_sample

TODAY = date(2026, 10, 24)
NOW = lambda: datetime(2026, 10, 24, 12, 0)  # noqa: E731
BATCH = "kicks-20261024-120000"


def setup(tmp_path, fake_client, n=3, owned=()):
    catalog = Catalog(tmp_path / "c.sqlite")
    samples = [fake_client.add(make_sample(i), owned=i in owned) for i in range(1, n + 1)]
    catalog.record_search(samples, "house kick", "kicks", 1, 50)
    catalog.mark_shortlisted([s.catalog_uuid for s in samples], BATCH)
    path = tmp_path / f"approved-{BATCH}.json"
    path.write_text(json.dumps({"batch_id": BATCH, "group": "kicks",
                                "catalog_uuids": [s.catalog_uuid for s in samples]}))
    return samples, catalog, path


def buy(fake_client, catalog, path, **kw):
    args = dict(confirm=True, max_credits=None, daily_cap=400, today=TODAY, staging=None, now=NOW)
    args.update(kw)
    return run_buy(fake_client, catalog, path, **args)


def purchased(fake_client):
    return [c[1] for c in fake_client.calls if c[0] == "purchase"]


def test_dry_run_spends_nothing(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    catalog.mark_licensed(samples[0].catalog_uuid)
    report = buy(fake_client, catalog, path, confirm=False)
    assert fake_client.count("purchase") == 0
    assert fake_client.count("asset_files") == 0
    assert report.dry_run is True
    assert report.planned == 3
    assert report.skipped_owned == 1
    assert report.bought == 0
    assert report.credits_before == 3360


def test_buy_rechecks_license_before_purchase(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    fake_client.owned.add(samples[1].catalog_uuid)  # owned on Splice, catalog does not know
    report = buy(fake_client, catalog, path)
    assert purchased(fake_client) == [samples[0].catalog_uuid, samples[2].catalog_uuid]
    assert report.skipped_owned == 1 and report.bought == 2
    assert report.stopped_reason is None
    assert catalog.get_by_catalog_uuid(samples[1].catalog_uuid).sample.licensed
    for s in samples:
        assert catalog.get_by_catalog_uuid(s.catalog_uuid).sample.licensed


def test_buy_stops_at_daily_cap_and_low_credits(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    for _ in range(399):
        catalog.log_purchase("x", "bought", None, None, NOW())
    report = buy(fake_client, catalog, path, daily_cap=400)
    assert report.bought == 1
    assert report.stopped_reason == "daily cap reached"

    fake_client2 = type(fake_client)()
    samples, catalog, path = setup(tmp_path / "b", fake_client2)
    fake_client2.credits = 1
    report = buy(fake_client2, catalog, path)
    assert report.bought == 1
    assert report.stopped_reason == "out of credits"
    assert len(purchased(fake_client2)) == 1


def test_buy_stops_on_auth_error_and_resume_skips_bought(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    fake_client.errors[("purchase", samples[1].catalog_uuid)] = AuthRejected("log in to splice.com again")
    report = buy(fake_client, catalog, path)
    assert report.bought == 1
    assert "log in to splice.com again" in report.stopped_reason
    assert purchased(fake_client) == [samples[0].catalog_uuid, samples[1].catalog_uuid]

    fake_client.calls.clear()
    report = buy(fake_client, catalog, path)
    assert purchased(fake_client) == [samples[1].catalog_uuid, samples[2].catalog_uuid]
    assert report.bought == 2 and report.skipped_owned == 1
    assert fake_client.credits == 3360 - 3


def test_uncertain_purchase_stops_and_is_logged(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    fake_client.errors[("purchase", samples[0].catalog_uuid)] = PurchaseUncertain("HTTP 503")
    report = buy(fake_client, catalog, path)
    assert report.uncertain == [samples[0].catalog_uuid]
    assert report.bought == 0
    assert "uncertain" in report.stopped_reason
    assert len(purchased(fake_client)) == 1  # not retried, run stopped
    rows = catalog.db.execute("SELECT catalog_uuid, result FROM purchases").fetchall()
    assert [tuple(r) for r in rows] == [(samples[0].catalog_uuid, "uncertain")]


def test_max_credits_cap(tmp_path, fake_client):
    _, catalog, path = setup(tmp_path, fake_client, n=5)
    report = buy(fake_client, catalog, path, max_credits=2)
    assert report.bought == 2
    assert len(purchased(fake_client)) == 2
    assert report.stopped_reason == "max credits for this run reached"


def test_downloads_each_purchase_when_staging_given(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    staging = tmp_path / "staging"
    report = buy(fake_client, catalog, path, staging=staging)
    assert report.bought == 3
    assert fake_client.count("download") == 3
    for s in samples:
        assert (staging / "Pack A" / "kicks" / s.basename).is_file()


def test_purchase_log_records_credits(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client, n=2)
    report = buy(fake_client, catalog, path)
    rows = catalog.db.execute("SELECT result, credits_before, credits_after FROM purchases ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("bought", 3360, 3359), ("bought", 3359, 3358)]
    assert report.credits_after == 3358
    assert catalog.purchases_on(TODAY) == 2


def test_refuses_sounds_not_in_the_batch(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    extra = fake_client.add(make_sample(50))
    catalog.record_search([extra], "q", "kicks", 1, 50)
    data = json.loads(path.read_text())
    data["catalog_uuids"].append(extra.catalog_uuid)
    data["catalog_uuids"].append("00000000-0000-4000-8000-0000000000ff")
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError) as exc:
        buy(fake_client, catalog, path)
    assert "2" in str(exc.value)
    assert fake_client.count("purchase") == 0


def test_false_purchase_result_stops(tmp_path, fake_client):
    samples, catalog, path = setup(tmp_path, fake_client)
    fake_client.purchase = lambda uuid: fake_client.calls.append(("purchase", uuid)) or False
    report = buy(fake_client, catalog, path)
    assert report.bought == 0
    assert len(purchased(fake_client)) == 1
    assert report.stopped_reason


def test_buy_command_dry_run_and_confirm(tmp_path, fake_client, cli_env, capsys):
    from splice_curator import cli

    samples, _, path = setup(tmp_path / "data_unused", fake_client)
    catalog = Catalog(tmp_path / "data" / "catalog.sqlite")
    catalog.record_search(samples, "q", "kicks", 1, 50)
    catalog.mark_shortlisted([s.catalog_uuid for s in samples], BATCH)
    catalog.close()

    assert cli.main(["--config", str(cli_env), "buy", str(path)]) == 0
    out = capsys.readouterr().out
    assert "dry run: no credits spent" in out
    assert "planned: 3" in out and "credits needed: 3" in out and "credits available: 3360" in out
    assert fake_client.count("purchase") == 0

    assert cli.main(["--config", str(cli_env), "buy", str(path), "--confirm",
                     "--max-credits", "1", "--no-download"]) == 0
    out = capsys.readouterr().out
    assert fake_client.count("purchase") == 1
    assert fake_client.count("download") == 0
    assert "bought 1" in out
