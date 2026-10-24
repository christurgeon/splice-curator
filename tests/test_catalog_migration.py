import sqlite3
from datetime import datetime

from splice_curator.catalog import UNRANKED, Catalog
from splice_curator.models import ChartLabel, ChartPack, Pack
from tests.helpers import make_sample

V1_SAMPLES = """
CREATE TABLE samples (
    file_hash TEXT PRIMARY KEY, catalog_uuid TEXT NOT NULL UNIQUE, name TEXT NOT NULL, pack TEXT, label TEXT,
    pack_genre TEXT, tags TEXT NOT NULL, bpm INTEGER, key TEXT, chord_type TEXT, duration_ms INTEGER,
    category TEXT NOT NULL, licensed INTEGER NOT NULL DEFAULT 0, preview_url TEXT, best_position INTEGER NOT NULL,
    best_query TEXT, local_path TEXT, shortlisted_batch TEXT, updated_at TEXT NOT NULL
);
"""
PACK = "aaaaaaaa-1111-4222-8333-444444444444"
NOW = datetime(2026, 10, 24, 12, 0)


def make_v1_db(path):
    db = sqlite3.connect(path)
    db.executescript(V1_SAMPLES)
    db.execute(
        "INSERT INTO samples VALUES ('h1','00000000-0000-4000-8000-000000000001','P/k.wav','P','L','house',"
        "'[[\"kicks\", null]]',NULL,NULL,NULL,400,'oneshot',1,NULL,7,'q','/x/k.wav','batch-1','2026-10-01T00:00:00')"
    )
    db.commit()
    db.close()


def test_migrates_a_v1_database_and_keeps_data(tmp_path):
    path = tmp_path / "old.sqlite"
    make_v1_db(path)
    cat = Catalog(path)
    row = cat.get_by_catalog_uuid("00000000-0000-4000-8000-000000000001")
    assert row.sample.licensed and row.best_position == 7 and row.shortlisted_batch == "batch-1"
    assert row.sample.pack_uuid is None and row.in_pack_position is None and row.price is None
    assert cat.db.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = {r["name"] for r in cat.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"packs", "pack_ranks", "chart_packs", "chart_labels"} <= tables


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite"
    make_v1_db(path)
    Catalog(path).close()
    cat = Catalog(path)
    assert cat.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_new_database_has_current_version(tmp_path):
    cat = Catalog(tmp_path / "new.sqlite")
    assert cat.db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_in_pack_and_query_positions_stay_separate(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    a, b = make_sample(1), make_sample(2)
    cat.record_search([a], "house kick", "kicks", 1, 50)
    cat.record_pack_samples([b, a], PACK, first_position=0, count=90)
    ra = cat.get_by_catalog_uuid(a.catalog_uuid)
    rb = cat.get_by_catalog_uuid(b.catalog_uuid)
    assert ra.best_position == 0 and ra.in_pack_position == 1 and ra.in_pack_count == 90
    assert rb.best_position == UNRANKED and rb.in_pack_position == 0
    assert ra.sample.pack_uuid == PACK
    cat.record_search([b], "other", "kicks", 1, 50)
    assert cat.get_by_catalog_uuid(b.catalog_uuid).best_position == 0
    assert cat.get_by_catalog_uuid(b.catalog_uuid).in_pack_position == 0


def test_discovered_rows_appear_in_rows_for_group_and_genre(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    s = make_sample(1)
    cat.record_pack_samples([s], PACK, 0, 10)
    assert cat.rows_for_group("kicks", "tech_house") == []
    cat.assign(s.file_hash, "kicks", "tech_house", "kick")
    (row,) = cat.rows_for_group("kicks", "tech_house")
    assert row.sample.catalog_uuid == s.catalog_uuid and ("tech_house", "kick") in row.genres
    assert cat.rows_for_group("kicks", "minimal") == []


def test_refresh_style_record_backfills_pack_uuid(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    s = make_sample(1)
    cat.record_search([s], "", None, 1, 50, ranked=False)
    assert cat.get_by_catalog_uuid(s.catalog_uuid).sample.pack_uuid is None
    s.pack_uuid = PACK
    cat.record_search([s], "", None, 1, 50, ranked=False)
    assert cat.get_by_catalog_uuid(s.catalog_uuid).sample.pack_uuid == PACK
    s.pack_uuid = None
    cat.record_search([s], "", None, 1, 50, ranked=False)
    assert cat.get_by_catalog_uuid(s.catalog_uuid).sample.pack_uuid == PACK  # not erased


def test_pack_and_chart_tables_round_trip(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    pack = Pack(PACK, "Heat", "tech house", "heat", "Solid Grooves", "2026-05-01", ["groovy"], 80)
    cat.upsert_packs([pack])
    cat.set_pack_rank(PACK, "tech_house", 3, None)
    cat.set_pack_rank(PACK, "tech_house", 4, 2)
    (rank,) = cat.pack_rank_rows()
    assert (rank["all_time_rank"], rank["recent_rank"]) == (4, 2)
    assert cat.pack_fetched_at(PACK) is None
    cat.mark_pack_fetched(PACK, NOW)
    assert cat.pack_fetched_at(PACK) == NOW

    assert cat.chart_fetched_at("global") is None
    cat.replace_chart_packs("global", [ChartPack(PACK, "Heat", "Solid Grooves", 3, None, "global")], NOW)
    cat.replace_chart_packs("global", [ChartPack(PACK, "Heat", "Solid Grooves", 1, 2, "global")], NOW)
    (cp,) = cat.chart_pack_rows()
    assert cp["position"] == 1 and cat.chart_fetched_at("global") == NOW
    cat.replace_chart_labels([ChartLabel("l1", "Solid Grooves", "solid-grooves", 2, 5, 120, ["house"])], NOW)
    assert cat.chart_fetched_at(None) == NOW and len(cat.chart_label_rows()) == 1


def test_set_price(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    s = make_sample(1)
    cat.record_search([s], "q", "kicks", 1, 50)
    cat.set_price(s.catalog_uuid, 1)
    assert cat.get_by_catalog_uuid(s.catalog_uuid).price == 1
