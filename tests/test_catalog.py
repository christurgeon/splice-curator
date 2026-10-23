from datetime import date, datetime, timedelta

from splice_curator.catalog import Catalog
from splice_curator.models import Sample


def make_sample(i=1, **kw) -> Sample:
    base = dict(
        file_hash=f"{i:064x}",
        catalog_uuid=f"00000000-0000-4000-8000-{i:012x}",
        name=f"Pack A/kicks/Kick_{i}.wav",
        pack="Pack A",
        label="Label A",
        pack_genre="house",
        tags=[("kicks", None), ("house", "Genre")],
        bpm=None,
        key=None,
        chord_type=None,
        duration_ms=500,
        category="oneshot",
        licensed=False,
        preview_url="https://x.s3.amazonaws.com/p.mp3",
    )
    base.update(kw)
    return Sample(**base)


def test_upsert_keeps_best_position_and_all_groups(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    others = [make_sample(100 + i) for i in range(3)]
    s = make_sample(1)
    cat.record_search(others + [s], "house kick", "kicks", page=2, page_size=50)
    row = cat.get_by_catalog_uuid(s.catalog_uuid)
    assert row.best_position == 53
    cat.record_search([s], "house drum loop", "drum_loops", page=1, page_size=50)
    row = cat.get_by_catalog_uuid(s.catalog_uuid)
    assert row.best_position == 0
    assert row.groups == {"kicks", "drum_loops"}
    cat.record_search(others + [s], "house kick", "kicks", page=2, page_size=50)
    assert cat.get_by_catalog_uuid(s.catalog_uuid).best_position == 0
    assert {r.sample.file_hash for r in cat.rows_for_group("drum_loops")} == {s.file_hash}
    assert len(cat.rows_for_group("kicks")) == 4


def test_record_search_without_group(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    cat.record_search([make_sample(1, licensed=True)], "", None, page=1, page_size=50)
    row = cat.get_by_catalog_uuid(make_sample(1).catalog_uuid)
    assert row.groups == set()


def test_licensed_flag_update_and_query(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    a, b = make_sample(1), make_sample(2, licensed=True)
    cat.record_search([a, b], "q", "kicks", 1, 50)
    assert [r.sample.catalog_uuid for r in cat.licensed_rows()] == [b.catalog_uuid]
    cat.mark_licensed(a.catalog_uuid)
    assert {r.sample.catalog_uuid for r in cat.licensed_rows()} == {a.catalog_uuid, b.catalog_uuid}
    # a later search that still says unlicensed does not undo a purchase
    cat.record_search([make_sample(1, licensed=False)], "q", "kicks", 1, 50)
    assert cat.get_by_catalog_uuid(a.catalog_uuid).sample.licensed is True


def test_shortlist_and_local_path(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    a = make_sample(1)
    cat.record_search([a], "q", "kicks", 1, 50)
    cat.mark_shortlisted([a.catalog_uuid], "kicks-001")
    cat.set_local_path(a.file_hash, tmp_path / "x.wav")
    row = cat.get_by_catalog_uuid(a.catalog_uuid)
    assert row.shortlisted_batch == "kicks-001"
    assert row.local_path == str(tmp_path / "x.wav")
    assert cat.get_by_catalog_uuid("00000000-0000-4000-8000-ffffffffffff") is None


def test_purchase_log_daily_count(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    now = datetime(2026, 10, 24, 12, 0)
    for _ in range(2):
        cat.log_purchase("u1", "bought", 10, 9, now)
    cat.log_purchase("u2", "uncertain", 9, None, now)
    cat.log_purchase("u3", "bought", 11, 10, now - timedelta(days=1))
    assert cat.purchases_on(date(2026, 10, 24)) == 2
    assert cat.purchases_on(date(2026, 10, 23)) == 1


def test_purchase_log_rejects_unknown_result(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    try:
        cat.log_purchase("u", "maybe", None, None, datetime.now())
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_tags_roundtrip(tmp_path):
    path = tmp_path / "c.sqlite"
    s = make_sample(1, tags=[("kicks", None), ("deep house", "Genre")], bpm=124, key="g#", chord_type="minor")
    Catalog(path).record_search([s], "q", "kicks", 1, 50)
    row = Catalog(path).get_by_catalog_uuid(s.catalog_uuid)
    assert row.sample == s
