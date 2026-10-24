from splice_curator.budget import get_genre, get_group
from splice_curator.catalog import CatalogRow
from splice_curator.rank import build_genre_shortlist, build_shortlist, near_dup_key, score, top_labels
from tests.helpers import make_sample

KICKS = get_group("kicks")


def row(i, pos, licensed=False, batch=None, **kw):
    return CatalogRow(make_sample(i, licensed=licensed, **kw), pos, {"kicks"}, None, batch)


def test_score_prefers_popular_and_genre():
    popular = row(1, 0, tags=[("kicks", None), ("house", "Genre")])
    plain = row(2, 10, tags=[("kicks", None)], pack_genre="hip hop")
    assert score(popular, KICKS, set()) > score(plain, KICKS, set())


def test_score_formula():
    r = row(1, 50, tags=[("kicks", None)], pack_genre="deep house", label="L")
    # 1/(1+1) + 0.2 genre (pack_genre) + 0.1 label + 0.1 want_tags
    assert abs(score(r, KICKS, {"L"}) - 0.9) < 1e-9
    r = row(2, 0, tags=[("snares", None)], pack_genre=None, label="L")
    assert abs(score(r, KICKS, set()) - 1.0) < 1e-9


def test_top_labels():
    rows = [row(i, i, label="A") for i in range(5)] + [row(10 + i, 10 + i, label="B") for i in range(2)]
    rows.append(row(99, 500, label="C"))
    assert top_labels(rows, n=1) == {"A"}
    assert top_labels(rows) == {"A", "B"}


def test_shortlist_excludes_owned_and_shortlisted():
    rows = [row(1, 0, licensed=True), row(2, 1, batch="kicks-1"), row(3, 2), row(4, 3)]
    picked = build_shortlist(rows, KICKS, size=10, per_pack_cap=5)
    assert [s.catalog_uuid for s in picked] == [rows[2].sample.catalog_uuid, rows[3].sample.catalog_uuid]


def test_shortlist_excludes_other_category():
    rows = [row(1, 0, category="loop"), row(2, 1)]
    assert [s.catalog_uuid for s in build_shortlist(rows, KICKS, 10, 5)] == [rows[1].sample.catalog_uuid]


def test_shortlist_per_pack_cap():
    rows = [row(i, i, pack="Same") for i in range(10)]
    assert len(build_shortlist(rows, KICKS, size=100, per_pack_cap=5)) == 5


def test_shortlist_size():
    rows = [row(i, i, pack=f"P{i}") for i in range(30)]
    picked = build_shortlist(rows, KICKS, size=7, per_pack_cap=5)
    assert len(picked) == 7
    assert picked[0].catalog_uuid == rows[0].sample.catalog_uuid


# -- genre shortlists ----------------------------------------------------------

TECH = get_genre("tech_house")
HATS = get_group("hats_cymbals")


def grow(i, pos, subtype="kick", genre="tech_house", group=KICKS, **kw):
    kw.setdefault("tags", [("tech house", "Genre")])
    kw.setdefault("pack", f"P{i}")
    kw.setdefault("name", f"P{i}/sound_{i}.wav")
    return CatalogRow(make_sample(i, **kw), pos, {group.id}, None, None, {(genre, subtype)})


def hashes(samples):
    return {s.file_hash for s in samples}


def test_genre_shortlist_requires_genre_match():
    rows = [
        grow(1, 0),
        grow(2, 1, tags=[("house", "Genre")], pack_genre="hip hop"),
        grow(3, 2, tags=[("kicks", None)], pack_genre="Tech House"),
    ]
    picked = build_genre_shortlist(rows, KICKS, TECH, 10, 5)
    assert hashes(picked) == {rows[0].sample.file_hash, rows[2].sample.file_hash}


def test_loop_bpm_window():
    drums = get_group("drum_loops")
    rows = [grow(i, i, subtype="drum loop", group=drums, category="loop", bpm=bpm)
            for i, bpm in enumerate([123, 124, 126, 128, 129, None], start=1)]
    picked = build_genre_shortlist(rows, drums, TECH, 10, 5)
    assert sorted(s.bpm for s in picked) == [124, 126, 128]


def test_subtype_quotas_and_spillover():
    rows = [grow(i, i, subtype="closed hat", group=HATS) for i in range(1, 6)]
    rows += [grow(10, 10, subtype="open hat", group=HATS)]
    rows += [grow(20 + i, 20 + i, subtype="ride", group=HATS) for i in range(5)]
    picked = hashes(build_genre_shortlist(rows, HATS, TECH, 6, 5))
    counts = {st: sum(1 for r in rows if r.sample.file_hash in picked and (("tech_house", st) in r.genres))
              for st in HATS.subtypes}
    assert counts == {"closed hat": 2, "open hat": 1, "ride": 3}


def test_leftover_fill_when_a_subtype_is_empty():
    rows = [grow(i, i, subtype="closed hat", group=HATS) for i in range(1, 8)]
    assert len(build_genre_shortlist(rows, HATS, TECH, 6, 5)) == 6


def test_per_label_cap():
    rows = [grow(i, i, label="Same") for i in range(10)]
    assert len(build_genre_shortlist(rows, KICKS, TECH, 10, 5, per_label_cap=3)) == 3


def test_near_duplicates_removed_same_pack_only():
    rows = [
        grow(1, 0, pack="A", name="A/Kick_01.wav"),
        grow(2, 1, pack="A", name="A/Kick_12.wav"),
        grow(3, 2, pack="B", name="B/kick-03.wav"),
    ]
    picked = build_genre_shortlist(rows, KICKS, TECH, 10, 5)
    assert hashes(picked) == {rows[0].sample.file_hash, rows[2].sample.file_hash}
    assert near_dup_key(rows[0].sample) == near_dup_key(rows[1].sample)
    assert near_dup_key(rows[0].sample) != near_dup_key(rows[2].sample)


def test_key_share_cap_for_bass_loops():
    bass = get_group("bass_loops")
    keyed = [grow(i, i, subtype="bass loop", group=bass, category="loop", bpm=126,
                  key="a", chord_type="minor") for i in range(1, 11)]
    unkeyed = [grow(20 + i, 20 + i, subtype="bass loop", group=bass, category="loop", bpm=126)
               for i in range(2)]
    picked = build_genre_shortlist(keyed + unkeyed, bass, TECH, 20, 5)
    assert sum(1 for s in picked if s.key) == 3  # ceil(0.15 * 20)
    assert sum(1 for s in picked if not s.key) == 2
