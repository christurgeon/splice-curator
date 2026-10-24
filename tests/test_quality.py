from datetime import datetime

import pytest

from splice_curator.catalog import UNRANKED, Catalog, CatalogRow
from splice_curator.config import load_config
from splice_curator.models import ChartLabel, ChartPack, Pack
from splice_curator.quality import (
    QualityContext, build_context, format_why, is_excluded, loop_shape_ok, percentile, score_row,
)
from tests.helpers import make_sample

NOW = datetime(2026, 10, 24)
PACK = "aaaaaaaa-0000-4000-8000-000000000001"


def ctx(**kw):
    return QualityContext(weights=dict(load_config(path=None).scoring), today=NOW, **kw)


def row(sample=None, best=UNRANKED, in_pack=None, count=None):
    return CatalogRow(sample or make_sample(1, pack_uuid=PACK), best, set(), None, None, set(), in_pack, count)


def score(r, group="kicks", genre="tech_house", c=None, kit=False):
    return score_row(r, group, genre, c or ctx(), kit)


def test_missing_signals_score_zero():
    assert score(row()).score == 0.0 and score(row()).why == {}


def test_in_pack_percentile_and_query_pop_are_separate():
    a = score(row(in_pack=0, count=100))
    b = score(row(in_pack=50, count=100))
    assert a.why["in_pack_pop"] == pytest.approx(1.0) and b.why["in_pack_pop"] == pytest.approx(0.5)
    assert "query_pop" not in a.why
    c = score(row(best=50))
    assert c.why["query_pop"] == pytest.approx(0.6 * 0.5) and "in_pack_pop" not in c.why


def test_pack_rank_signals_use_genre():
    c = ctx(pack_ranks={(PACK, "tech_house"): (25, 0)})
    s = score(row(), c=c)
    assert s.why["pack_pop"] == pytest.approx(0.4 * 0.5)
    assert s.why["pack_recent_pop"] == pytest.approx(0.5 * 1.0)
    assert "pack_pop" not in score(row(), genre="deep_house", c=c).why


def test_trending_rising_new_and_label_chart():
    c = ctx(chart_packs={PACK: [(1, 3), (10, None)]}, chart_labels={"label a": (11, 4)})
    s = score(row(), c=c)
    assert s.why["trending"] == pytest.approx(0.4 * (1.0 + 0.2))  # best chart entry, rising
    assert s.why["label_chart"] == pytest.approx(0.2 * (1 - 10 / 50))
    falling = score(row(), c=ctx(chart_packs={PACK: [(1, 1)]}))
    assert falling.why["trending"] == pytest.approx(0.4)
    new = score(row(), c=ctx(chart_packs={PACK: [(1, None)]}))
    assert new.why["trending"] == pytest.approx(0.4 * 1.2)


def test_fresh_linear_over_24_months():
    new = score(row(), c=ctx(pack_published={PACK: datetime(2026, 10, 24)}))
    half = score(row(), c=ctx(pack_published={PACK: datetime(2025, 10, 24)}))
    old = score(row(), c=ctx(pack_published={PACK: datetime(2020, 1, 1)}))
    assert new.why["fresh"] == pytest.approx(0.3)
    assert half.why["fresh"] == pytest.approx(0.3 * (1 - 12 / 24), abs=0.01)
    assert "fresh" not in old.why


def test_sonic_bonus_from_sample_or_pack_tags():
    assert score(row(make_sample(1, tags=[("kicks", None), ("punchy", None)], pack_uuid=PACK))).why["sonic_bonus"] == 0.1
    assert score(row(), c=ctx(pack_tags={PACK: {"groovy"}})).why["sonic_bonus"] == 0.1


def test_owner_affinity_pack_label_and_off_genre():
    c = ctx()
    c.owned_packs[PACK] = 3
    assert score(row(), c=c).why["owner_affinity"] == 0.15
    c2 = ctx()
    c2.owned_labels["label a"] = 5
    assert score(row(), c=c2).why["owner_affinity"] == pytest.approx(0.075)
    c3 = ctx()
    c3.owned_packs[PACK] = 9
    off = make_sample(1, pack_uuid=PACK, pack_genre="trap")
    assert "owner_affinity" not in score(row(off), c=c3).why


def test_kit_bonus_only_when_flagged():
    assert "kit_bonus" not in score(row()).why
    assert score(row(), kit=True).why["kit_bonus"] == 0.1


def test_penalties():
    trap = make_sample(1, pack_genre="Trap", pack_uuid=PACK)
    assert score(row(trap)).why["off_genre_penalty"] == -0.6
    bad = make_sample(1, name="P/808_Kick.wav", pack_uuid=PACK)
    assert score(row(bad)).why["bad_word_penalty"] == -0.4
    hip = make_sample(1, tags=[("hip hop", "Genre")], pack_uuid=PACK)
    assert score(row(hip)).why["bad_word_penalty"] == -0.4
    riser = make_sample(1, category="loop", name="P/Riser_Loop.wav", tags=[("fx", None)], bpm=126)
    assert score(row(riser), group="chord_loops").why["loop_junk_penalty"] == -0.3
    one_shot_riser = make_sample(1, name="P/Riser.wav")
    assert "loop_junk_penalty" not in score(row(one_shot_riser), group="fx_vocal_oneshots").why


def test_drum_loop_full_vs_tops():
    full = make_sample(1, category="loop", name="P/Full_Drum_Loop.wav", tags=[("drums", None)])
    tops = make_sample(2, category="loop", name="P/Top_Loop_01.wav", tags=[("drums", None)])
    nokick = make_sample(3, category="loop", name="P/Drums_No_Kick.wav", tags=[("drums", None)])
    assert score(row(full), group="drum_loops").why["full_loop_penalty"] == -0.2
    assert score(row(tops), group="drum_loops").why["tops_bonus"] == 0.2
    assert score(row(nokick), group="drum_loops").why["tops_bonus"] == 0.2
    assert "full_loop_penalty" not in score(row(full), group="perc_loops").why


def test_prefer_and_exclude():
    c = ctx(prefer_labels=frozenset({"label a"}), exclude_packs=frozenset({"pack a"}))
    assert score(row(), c=c).why["prefer_bonus"] == 0.3
    assert is_excluded(make_sample(1), c)
    assert not is_excluded(make_sample(1, pack="Other", label="Other"), c)
    kshmr = ctx(exclude_pack_patterns=("sounds of kshmr",))
    assert is_excluded(make_sample(1, pack="Sounds of KSHMR Vol 5 - FX"), kshmr)
    assert not is_excluded(make_sample(1, pack="KSHMR Essentials"), kshmr)


def test_weights_are_configurable():
    c = ctx()
    c.weights["in_pack_pop"] = 2.0
    assert score(row(in_pack=0, count=10), c=c).score == pytest.approx(2.0)


@pytest.mark.parametrize("bpm, ms, ok", [
    (126, 7619, True),    # 4 bars at 126 = 7619 ms
    (126, 3810, True),    # 2 bars
    (126, 15238, True),   # 8 bars
    (126, 7619 * 1.04, True),
    (126, 7619 * 1.2, False),
    (126, 5714, False),   # 3 bars
    (None, 7619, False),
    (126, None, False),
])
def test_loop_bar_rule(bpm, ms, ok):
    assert loop_shape_ok(make_sample(1, category="loop", bpm=bpm, duration_ms=ms and int(ms))) is ok


def test_percentile():
    assert percentile([], 40) == 0.0
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([0, 10], 25) == 2.5
    assert percentile([5], 90) == 5


def test_format_why_orders_by_strength():
    assert format_why({"a": 0.1, "b": -0.6, "c": 0.3}) == "b=-0.60 c=+0.30 a=+0.10"


def test_build_context_reads_catalog(tmp_path):
    cat = Catalog(tmp_path / "c.sqlite")
    cfg = load_config(path=None, overrides={"prefer_labels": ["Label A"]})
    cat.upsert_packs([Pack(PACK, "Pack A", "tech house", "p", "Label A", "2026-08-01T00:00:00Z", ["Groovy"], 5)])
    cat.set_pack_rank(PACK, "tech_house", 2, 1)
    cat.replace_chart_packs("global", [ChartPack(PACK, "Pack A", "Label A", 7, 9, "global")], NOW)
    cat.replace_chart_labels([ChartLabel("l", "Label A", "label-a", 3, 3, 10, [])], NOW)
    owned = [make_sample(i, licensed=True, pack_uuid=None) for i in range(1, 4)]  # old rows: matched by name
    cat.record_search(owned, "", None, 1, 50, ranked=False)
    c = build_context(cat, cfg, today=NOW)
    assert c.pack_ranks[(PACK, "tech_house")] == (2, 1)
    assert c.chart_packs[PACK] == [(7, 9)] and c.chart_labels["label a"] == (3, 3)
    assert c.pack_tags[PACK] == {"groovy"} and c.pack_published[PACK].year == 2026
    assert c.owned_packs[PACK] == 3 and c.owned_labels["label a"] == 3
    assert c.prefer_labels == {"label a"} and c.exclude_pack_patterns == ("sounds of kshmr",)
    s = make_sample(9, pack_uuid=None)
    assert c.pack_uuid(s) == PACK


@pytest.mark.parametrize("genre, off", [
    ("tech house", False), ("deep house", False), ("nu disco", False), ("minimal techno", False),
    ("bass house", False), ("house", False), (None, False), ("", False),
    ("rnb", True), ("soul", True), ("cinematic", True), ("trap", True), ("uk garage", True),
    ("hard techno", True), ("hyper techno", True), ("afropop & afrobeats", True), ("drums", False), ("edm", False),
])
def test_is_off_genre(genre, off):
    from splice_curator.quality import is_off_genre

    assert is_off_genre(genre) is off
