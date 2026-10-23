from splice_curator.budget import get_group
from splice_curator.catalog import CatalogRow
from splice_curator.rank import build_shortlist, score, top_labels
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
