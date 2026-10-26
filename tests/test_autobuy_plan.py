import json
from datetime import date, datetime

import pytest

from splice_curator import cli
from splice_curator.config import load_config
from splice_curator.planfiles import estimate_bytes_per_sound, load_plan, save_plan, schedule_lines
from splice_curator.review import load_approved, load_rejected
from splice_curator.selection import make_plan
from tests.test_selection import NOW, World


def build(tmp_path, packs=60, **cfg):
    w = World(tmp_path, db=tmp_path / "data" / "catalog.sqlite", **cfg)
    for p in range(1, packs):
        for g in ("kicks", "snares_claps", "drum_loops"):
            w.add(g, pack=p, n=3)
    return w


def run_plan(cli_env, *extra):
    return cli.main(["--config", str(cli_env), "autobuy", "plan", *extra])


def test_dry_run_prints_summary_and_saves_nothing(tmp_path, cli_env, fake_client, capsys):
    w = build(tmp_path)
    fake_client.credits = 500
    assert run_plan(cli_env) == 0
    out = capsys.readouterr().out
    assert "plan: " in out and "keep 200" in out and "capacity" in out
    assert "from charted packs" in out and "score percentiles" in out and "latest safe start" in out
    assert "dry run: nothing saved" in out
    assert w.cat.pending_batches() == {}
    assert not (tmp_path / "data" / "plans").exists()


def test_plan_total_is_balance_minus_keep_on_the_real_path(tmp_path, cli_env, fake_client, capsys):
    build(tmp_path, packs=100)
    fake_client.credits = 400
    assert run_plan(cli_env) == 0
    assert "plan: 200 credits (balance 400, keep 200, spendable 200" in capsys.readouterr().out


def test_confirm_saves_files_and_marks_batches(tmp_path, cli_env, fake_client, capsys):
    w = build(tmp_path)
    fake_client.credits = 400
    assert run_plan(cli_env, "--confirm", "--page") == 0
    out = capsys.readouterr().out
    assert "plan saved" in out and "spot-check page" in out
    data = tmp_path / "data"
    plan_path = next((data / "plans").glob("plan-*.json"))
    plan = load_plan(plan_path)
    assert len(plan.items) == 200 and [i["order"] for i in plan.items] == list(range(1, 201))
    csv_text = next((data / "plans").glob("plan-*.csv")).read_text()
    assert csv_text.splitlines()[0] == "group,genre,pack,label,score,why,link"
    assert "https://splice.com/sounds/sample/" in csv_text and "in_pack_pop=" in csv_text
    for path in (data / "approved").glob("approved-*.json"):
        batch, group, uuids = load_approved(path)
        assert batch.endswith("-ab") and uuids
    pending = w.cat.pending_batches()
    assert sum(pending.values()) == 200
    page = next((data / "review").glob("plan-*.html")).read_text()
    assert "in_pack_pop" in page and "Export rejected list" in page and "rejected-" in page


def test_plan_twice_without_replace_counts_pending_with_replace_is_identical(tmp_path, cli_env, fake_client, capsys):
    build(tmp_path, packs=100)
    fake_client.credits = 400
    run_plan(cli_env, "--confirm")
    first = sorted(next((tmp_path / "data" / "plans").glob("plan-*.json")).read_text().split())
    capsys.readouterr()
    run_plan(cli_env)
    assert "plan: 0 credits" in capsys.readouterr().out  # all 200 spendable credits are already pending
    run_plan(cli_env, "--replace")
    out = capsys.readouterr().out
    assert "would release 200" in out and "plan: 200 credits" in out
    assert first  # dry run changed nothing
    from splice_curator.catalog import Catalog

    assert sum(Catalog(tmp_path / "data" / "catalog.sqlite").pending_batches().values()) == 200


def test_release_and_rejected(tmp_path, cli_env, fake_client, capsys):
    w = build(tmp_path, packs=100)
    fake_client.credits = 400
    run_plan(cli_env, "--confirm")
    plan = load_plan(next((tmp_path / "data" / "plans").glob("plan-*.json")))
    rejected = tmp_path / "rejected-x.json"
    rejected.write_text(json.dumps({"plan_id": plan.plan_id, "rejected": plan.uuids[:5]}))
    assert load_rejected(rejected) == plan.uuids[:5]
    assert cli.main(["--config", str(cli_env), "release", "--rejected", str(rejected)]) == 0
    assert "rejected 5" in capsys.readouterr().out
    assert cli.main(["--config", str(cli_env), "release"]) == 0
    assert "released 195" in capsys.readouterr().out
    # rejected sounds stay out of every later plan
    run_plan(cli_env, "--confirm")
    new = load_plan(sorted((tmp_path / "data" / "plans").glob("plan-*.json"))[-1])
    assert not set(new.uuids) & set(plan.uuids[:5])


def test_single_group_plan(tmp_path, cli_env, fake_client, capsys):
    build(tmp_path, packs=100)
    fake_client.credits = 1000
    assert run_plan(cli_env, "--group", "kicks", "--confirm") == 0
    plan = load_plan(next((tmp_path / "data" / "plans").glob("plan-*.json")))
    assert {i["group"] for i in plan.items} == {"kicks"}
    assert cli.main(["--config", str(cli_env), "autobuy", "plan", "--group", "nope"]) == 1


def test_load_plan_validation(tmp_path):
    p = tmp_path / "p.json"
    for bad in ({}, {"plan_id": "x", "items": [{"catalog_uuid": "no"}]}, {"plan_id": "", "items": []}):
        p.write_text(json.dumps(bad))
        try:
            load_plan(p)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_schedule_lines_and_storage_estimate(tmp_path):
    cfg = load_config(path=None, overrides={"deadline": "2026-11-06"})
    lines = schedule_lines(800, cfg, date(2026, 10, 24))
    assert "2 buying days at 400/day" in lines[0] and "latest safe start: 2026-11-04" in lines[1]
    behind = schedule_lines(3000, cfg, date(2026, 11, 1))
    assert any("BEHIND" in line and "600/day" in line for line in behind)
    assert estimate_bytes_per_sound(tmp_path / "none") == 1_500_000
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.wav").write_bytes(b"0" * 1000)
    (tmp_path / "a" / "y.wav").write_bytes(b"0" * 3000)
    assert estimate_bytes_per_sound(tmp_path) == 2000


def test_save_plan_marks_only_its_items(tmp_path):
    w = build(tmp_path, packs=40)
    plan = make_plan(w.cat, w.cfg, w.ctx(), 100, NOW)
    save_plan(w.cat, plan, w.cfg)
    assert sum(w.cat.pending_batches().values()) == plan.total == 100
    assert datetime(2026, 10, 24, 12, 0) == plan.created


def test_schedule_without_deadline_shows_days_only():
    from datetime import date
    from splice_curator.config import load_config
    from splice_curator.planfiles import schedule_lines
    lines = schedule_lines(1000, load_config(path=None, overrides={"max_purchases_per_day": 500}), date(2026, 1, 1))
    assert lines == ["buying: 1000 credits = 2 buying days at 500/day (about 7.6 h in all); no deadline set"]


def test_seconds_per_sound_follows_the_delays():
    from splice_curator.planfiles import seconds_per_sound
    assert seconds_per_sound(load_config(path=None)) == pytest.approx(27.5)
    fast = load_config(path=None, overrides={"delays": {"asset": {"min_s": 1.5, "max_s": 2.5},
                                                        "purchase": {"min_s": 3, "max_s": 5},
                                                        "download": {"min_s": 1, "max_s": 2}}})
    assert seconds_per_sound(fast) == pytest.approx(15.5)  # measured about 15 s on 2026-10-25
