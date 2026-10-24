import json
from datetime import datetime, timedelta

import pytest

from splice_curator import cli
from splice_curator.buy import buy_lock
from splice_curator.catalog import Catalog
from splice_curator.planfiles import PlanFile
from splice_curator.runner import run_autobuy, seconds_until_midnight
from tests.helpers import make_sample

START = datetime(2026, 10, 24, 20, 0)


class Clock:
    def __init__(self):
        self.t = START
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


def setup(tmp_path, fake_client, n=5, db=None):
    catalog = Catalog(db or tmp_path / "c.sqlite")
    samples = [fake_client.add(make_sample(i)) for i in range(1, n + 1)]
    catalog.record_search(samples, "q", "kicks", 1, 50)
    items = []
    for order, s in enumerate(reversed(samples), 1):  # plan order differs from catalog order
        batch = f"kicks-tech_house-20261024-100000-ab"
        items.append({"order": order, "batch_id": batch, "group": "kicks", "genre": "tech_house",
                      "catalog_uuid": s.catalog_uuid, "price": 1, "score": 1.0})
    catalog.mark_shortlisted([s.catalog_uuid for s in samples], "kicks-tech_house-20261024-100000-ab")
    return samples, catalog, PlanFile("20261024-100000", items)


def go(fake_client, catalog, plan, clock=None, **kw):
    clock = clock or Clock()
    args = dict(confirm=True, max_credits=None, daily_cap=400, staging=None, now=clock.now, sleep=clock.sleep)
    args.update(kw)
    return run_autobuy(fake_client, catalog, plan, **args)


def bought(fake_client):
    return [c[1] for c in fake_client.calls if c[0] == "purchase"]


def test_dry_run_spends_nothing(tmp_path, fake_client):
    samples, catalog, plan = setup(tmp_path, fake_client)
    catalog.mark_licensed(samples[0].catalog_uuid)
    result = go(fake_client, catalog, plan, confirm=False)
    assert result.buy.dry_run and result.buy.planned == 5 and result.buy.skipped_owned == 1
    assert result.remaining == 4
    assert fake_client.count("purchase") == 0 and fake_client.count("asset_files") == 0


def test_buys_in_plan_order(tmp_path, fake_client):
    samples, catalog, plan = setup(tmp_path, fake_client)
    result = go(fake_client, catalog, plan)
    assert bought(fake_client) == [s.catalog_uuid for s in reversed(samples)]
    assert result.buy.bought == 5 and result.buy.credits_spent == 5 and result.remaining == 0
    assert result.buy.credits_after == 3355


def test_rejected_are_subtracted(tmp_path, fake_client):
    samples, catalog, plan = setup(tmp_path, fake_client)
    result = go(fake_client, catalog, plan, rejected={samples[0].catalog_uuid, samples[1].catalog_uuid})
    assert result.skipped_rejected == 2 and result.buy.bought == 3
    assert samples[0].catalog_uuid not in bought(fake_client)


def test_stale_plan_is_refused(tmp_path, fake_client):
    samples, catalog, plan = setup(tmp_path, fake_client)
    catalog.release_pending(batch_id="kicks-tech_house-20261024-100000-ab")
    with pytest.raises(ValueError, match="autobuy plan"):
        go(fake_client, catalog, plan)
    assert fake_client.count("purchase") == 0


def test_stops_at_daily_cap_without_until_done(tmp_path, fake_client):
    _, catalog, plan = setup(tmp_path, fake_client)
    result = go(fake_client, catalog, plan, daily_cap=2)
    assert result.buy.bought == 2 and result.buy.stopped_reason == "daily cap reached"
    assert result.remaining == 3


def test_until_done_sleeps_to_midnight_and_resumes(tmp_path, fake_client):
    _, catalog, plan = setup(tmp_path, fake_client)
    clock = Clock()
    logs = []
    result = go(fake_client, catalog, plan, clock=clock, daily_cap=2, until_done=True, log=logs.append)
    assert result.buy.bought == 5 and result.buy.stopped_reason is None and result.remaining == 0
    assert len(clock.slept) == 2
    assert clock.slept[0] == seconds_until_midnight(START)
    assert [n for _, n in result.days] == [2, 2, 1]
    assert [d.isoformat() for d, _ in result.days] == ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert any("waiting for the next day" in m for m in logs)
    days = [r["day"] for r in catalog.db.execute("SELECT day FROM purchases WHERE result = 'bought' ORDER BY id")]
    assert days == ["2026-10-24"] * 2 + ["2026-10-25"] * 2 + ["2026-10-26"]


def test_until_done_still_obeys_max_credits_and_other_stops(tmp_path, fake_client):
    samples, catalog, plan = setup(tmp_path, fake_client)
    result = go(fake_client, catalog, plan, max_credits=3, until_done=True)
    assert result.buy.bought == 3 and result.buy.stopped_reason == "max credits for this run reached"
    fake_client.credits = 0
    result = go(fake_client, catalog, plan, until_done=True)
    assert result.buy.stopped_reason == "out of credits"


def test_progress_logged_every_ten(tmp_path, fake_client):
    _, catalog, plan = setup(tmp_path, fake_client, n=21)
    logs = []
    go(fake_client, catalog, plan, log=logs.append)
    assert [m for m in logs if m.startswith("bought ")] and len([m for m in logs if m.startswith("bought ")]) == 2
    assert "to go" in logs[0] and "min left" in logs[0]


def test_lock_blocks_second_runner(tmp_path, fake_client):
    _, catalog, plan = setup(tmp_path, fake_client)
    with buy_lock(catalog.path.parent):
        with pytest.raises(ValueError, match="another buy"):
            go(fake_client, catalog, plan)
    assert fake_client.count("purchase") == 0


def test_resume_after_stop_does_not_rebuy(tmp_path, fake_client):
    _, catalog, plan = setup(tmp_path, fake_client)
    go(fake_client, catalog, plan, daily_cap=2)
    fake_client.calls.clear()
    clock = Clock()
    clock.t = START + timedelta(days=1)
    result = go(fake_client, catalog, plan, clock=clock, daily_cap=2)
    assert result.buy.bought == 2 and result.buy.skipped_owned == 2
    assert fake_client.count("asset_files") == 2  # owned rows cost no request
    assert len(set(bought(fake_client))) == 2


def test_zero_daily_cap_is_refused(tmp_path, fake_client):
    _, catalog, plan = setup(tmp_path, fake_client)
    with pytest.raises(ValueError):
        go(fake_client, catalog, plan, daily_cap=0, until_done=True)


def test_cli_run_dry_then_confirm_with_rejected_file(tmp_path, cli_env, fake_client, capsys):
    samples, catalog, plan = setup(tmp_path, fake_client, db=tmp_path / "data" / "catalog.sqlite")
    plan_path = tmp_path / "plans" / f"plan-{plan.plan_id}.json"
    plan_path.parent.mkdir()
    plan_path.write_text(json.dumps({"plan_id": plan.plan_id, "items": plan.items}))
    (plan_path.parent / f"rejected-{plan.plan_id}.json").write_text(
        json.dumps({"plan_id": plan.plan_id, "rejected": [samples[0].catalog_uuid]}))
    catalog.close()

    assert cli.main(["--config", str(cli_env), "autobuy", "run", str(plan_path)]) == 0
    out = capsys.readouterr().out
    assert "subtracting 1 rejected" in out and "planned: 4" in out and "credits needed: 4" in out
    assert "dry run: no credits spent" in out and fake_client.count("purchase") == 0

    assert cli.main(["--config", str(cli_env), "autobuy", "run", str(plan_path), "--confirm",
                     "--max-credits", "2", "--no-download"]) == 0
    out = capsys.readouterr().out
    assert fake_client.count("purchase") == 2 and "bought 2 (2 credits)" in out and "left in plan 2" in out
