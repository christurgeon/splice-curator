from collections import Counter
from datetime import datetime

import pytest

from splice_curator.budget import GROUPS, get_group
from splice_curator.catalog import Catalog
from splice_curator.config import load_config
from splice_curator.quality import QualityContext
from splice_curator.selection import AUTOBUY_SUFFIX, make_plan, order_items
from tests.helpers import make_sample

NOW = datetime(2026, 10, 24, 12, 0)
GENRE = "tech_house"


def pu(i):
    return f"aaaaaaaa-0000-4000-8000-{i:012x}"


class World:
    """A catalog plus a counter that hands out unique sounds."""

    def __init__(self, tmp_path, db=None, **cfg):
        self.cat = Catalog(db or tmp_path / "c.sqlite")
        self.cfg = load_config(path=None, overrides={"data_dir": tmp_path / "d", "keep": 0, **cfg})
        self.n = 0
        self.ranks = {}

    def add(self, group="kicks", genre=GENRE, pack=1, label=None, n=1, subtype=None, **kw):
        g = get_group(group)
        for k in range(n):
            self.n += 1
            tags = kw.pop("tags", None) or [(next(iter(g.want_tags)), None), ("tech house", "Genre")]
            extra = dict(
                name=f"Pack {pack}/Sound_{self.n}_{'x' * (self.n % 7)}.wav", pack=f"Pack {pack}",
                label=label or f"Label {pack}", pack_genre="tech house", tags=tags, pack_uuid=pu(pack),
                category=g.category, duration_ms=500 + self.n,
            )
            if g.category == "loop":
                extra.update(bpm=126, duration_ms=7619)
            extra.update(kw)
            s = make_sample(self.n, **extra)
            self.cat.record_pack_samples([s], pu(pack), self.n, 1000)
            self.cat.assign(s.file_hash, group, genre, subtype or g.subtypes[0])
            self.ranks.setdefault((pu(pack), genre), (pack, None))

    def ctx(self):
        c = QualityContext(weights=dict(self.cfg.scoring), today=NOW)
        c.pack_ranks = dict(self.ranks)
        c.exclude_labels = frozenset(x.lower() for x in self.cfg.exclude_labels)
        return c

    def plan(self, balance, **kw):
        return make_plan(self.cat, self.cfg, self.ctx(), balance, NOW)


def by_group(plan):
    return Counter(i.group_id for i in plan.items)


def test_plan_total_equals_balance_minus_keep(tmp_path):
    w = World(tmp_path, keep=40)
    for p in range(1, 60):
        for g in ("kicks", "snares_claps", "bass_oneshots"):
            w.add(g, pack=p, n=3)
    plan = w.plan(balance=240)
    assert plan.spendable == 200 and plan.total == 200 and plan.unplaced == 0
    assert len({i.row.sample.file_hash for i in plan.items}) == 200


def test_group_targets_scale_to_spendable(tmp_path):
    w = World(tmp_path)
    for p in range(1, 80):
        for g in GROUPS:
            w.add(g.id, pack=p, n=2)
    plan = w.plan(balance=1000)
    assert plan.total == 1000 and plan.unplaced == 0
    cap = {(c.group_id): 0 for c in plan.capacity}
    for c in plan.capacity:
        cap[c.group_id] += c.quota
    assert sum(cap.values()) == round(1000 * 3160 / 3360)
    assert cap["drum_loops"] > cap["vocal_loops"] > 0


def test_capacity_shortfall_report_and_final_pass(tmp_path):
    w = World(tmp_path)
    for p in range(1, 40):
        w.add("kicks", pack=p, n=4)  # only kicks have candidates
    plan = w.plan(balance=400)
    kicks_th = next(c for c in plan.capacity if (c.group_id, c.genre_id) == ("kicks", GENRE))
    assert kicks_th.candidates == 39 * 4
    empty = next(c for c in plan.capacity if (c.group_id, c.genre_id) == ("bass_loops", GENRE))
    assert empty.candidates == 0 and empty.picked == 0 and empty.shortfall == empty.quota > 0
    # final pass puts the unfilled credits on the best remaining sounds; caps still apply
    assert plan.total <= 39 * 5
    assert all(i.group_id == "kicks" for i in plan.items)
    assert any(i.final_pass for i in plan.items)


def test_unplaced_credits_are_reported_with_reason(tmp_path):
    w = World(tmp_path)
    w.add("kicks", pack=1, n=30)
    plan = w.plan(balance=500)
    assert plan.total == 5  # per-pack cap per batch
    assert plan.unplaced == 495 and "caps" in plan.unplaced_reason
    w2 = World(tmp_path / "e")
    plan2 = w2.plan(balance=100)
    assert plan2.total == 0 and plan2.unplaced == 100 and "no candidates" in plan2.unplaced_reason


def test_caps_per_pack_group_total_and_label(tmp_path):
    w = World(tmp_path, per_pack_cap=3, max_per_pack_per_group=4, max_per_pack_total=6)
    for g in ("kicks", "snares_claps"):
        for genre in ("tech_house", "minimal"):
            w.add(g, genre=genre, pack=1, n=8)
        for p in range(2, 9):
            w.add(g, pack=p, n=6)
    plan = w.plan(balance=600)
    per_pack = Counter(i.row.sample.pack for i in plan.items)
    assert max(per_pack.values()) <= 6
    per_group_pack = Counter((i.group_id, i.row.sample.pack) for i in plan.items)
    assert max(per_group_pack.values()) <= 4
    per_batch_pack = Counter((i.group_id, i.genre_id, i.row.sample.pack) for i in plan.items)
    assert max(per_batch_pack.values()) <= 3


def test_label_share_cap_per_group(tmp_path):
    w = World(tmp_path, per_pack_cap=50, max_per_pack_per_group=50, max_per_pack_total=50, per_label_share=0.15)
    for p in range(1, 40):
        w.add("kicks", pack=p, label="Big Label", n=3)
    for p in range(40, 80):
        w.add("kicks", pack=p, n=3)
    plan = w.plan(balance=300)
    kicks = [i for i in plan.items if i.group_id == "kicks"]
    big = [i for i in kicks if i.row.sample.label == "Big Label"]
    target = round(300 * 3160 / 3360 * 300 / 3160)
    assert len(big) <= -(-15 * target // 100)


def test_near_duplicates_and_cross_pack_duplicates_skipped(tmp_path):
    w = World(tmp_path)
    w.add("kicks", pack=1, name="Pack 1/Kick_Deep_wet.wav", duration_ms=700)
    w.add("kicks", pack=1, name="Pack 1/Kick_Deep_dry.wav", duration_ms=701)  # variant of the first
    w.add("kicks", pack=1, name="Pack 1/Kick_Deep_2.wav", duration_ms=702)  # digit kept: distinct
    w.add("kicks", pack=2, name="Pack 2/Kick_Comp.wav", duration_ms=900)
    w.add("kicks", pack=3, name="Pack 3/KICK_COMP.wav", duration_ms=900)  # same sound in a compilation
    plan = w.plan(balance=100)
    names = sorted(i.row.sample.basename.lower() for i in plan.items)
    assert names.count("kick_comp.wav") == 1
    assert not ("kick_deep_wet.wav" in names and "kick_deep_dry.wav" in names)
    assert "kick_deep_2.wav" in names


def test_owned_and_pending_and_excluded_are_not_candidates(tmp_path):
    w = World(tmp_path, exclude_labels=["Bad"])
    w.add("kicks", pack=1, n=3)
    w.add("kicks", pack=2, label="Bad", n=3)
    w.add("kicks", pack=3, n=3)
    rows = w.cat.rows_for_group("kicks", GENRE)
    owned = [r for r in rows if r.sample.pack == "Pack 1"][0]
    w.cat.mark_licensed(owned.sample.catalog_uuid)
    pend = [r for r in rows if r.sample.pack == "Pack 3"][0]
    w.cat.mark_shortlisted([pend.sample.catalog_uuid], "other-batch")
    plan = w.plan(balance=300)
    picked = {i.row.sample.catalog_uuid for i in plan.items}
    assert owned.sample.catalog_uuid not in picked and pend.sample.catalog_uuid not in picked
    assert all(i.row.sample.label != "Bad" for i in plan.items)


def test_loop_rules_bpm_window_and_bars(tmp_path):
    w = World(tmp_path)
    w.add("bass_loops", pack=1, tags=[("bass", None)], bpm=126, duration_ms=7619)
    w.add("bass_loops", pack=2, tags=[("bass", None)], bpm=100, duration_ms=9600)  # out of the BPM window
    w.add("bass_loops", pack=3, tags=[("bass", None)], bpm=126, duration_ms=5714)  # 3 bars
    plan = w.plan(balance=100)
    assert [i.row.sample.pack for i in plan.items] == ["Pack 1"]


def test_quota_subtracts_bought_and_pending_only(tmp_path):
    w = World(tmp_path)
    for p in range(1, 60):
        w.add("kicks", pack=p, n=3)
    first = w.plan(balance=600)
    cell = next(c for c in first.capacity if (c.group_id, c.genre_id) == ("kicks", GENRE))
    # mark the plan's kicks as pending in their batches: the next plan has a smaller quota
    picks = [i for i in first.items if (i.group_id, i.genre_id) == ("kicks", GENRE) and not i.final_pass]
    assert len(picks) == cell.quota
    w.cat.mark_shortlisted([i.row.sample.catalog_uuid for i in picks], picks[0].batch_id)
    second = w.plan(balance=600)
    cell2 = next(c for c in second.capacity if (c.group_id, c.genre_id) == ("kicks", GENRE))
    assert cell2.pending == len(picks) and cell2.quota == cell.quota - len(picks)
    assert second.pending == len(picks)
    assert second.total + second.unplaced == 600 - len(picks)
    # releasing the pending sounds restores the first plan exactly
    manual = w.cat.rows_for_group("kicks", GENRE)[-1]
    assert manual.shortlisted_batch is None
    assert w.cat.release_pending(suffix=AUTOBUY_SUFFIX) == len(picks)
    again = w.plan(balance=600)
    assert [i.row.sample.catalog_uuid for i in again.items] == [i.row.sample.catalog_uuid for i in first.items]


def test_bought_sounds_raise_total_target_not_the_plan(tmp_path):
    w = World(tmp_path)
    for p in range(1, 120):
        w.add("kicks", pack=p, n=3)
    plan = w.plan(balance=200)
    picks = [i for i in plan.items if (i.group_id, i.genre_id) == ("kicks", GENRE)][:10]
    w.cat.mark_shortlisted([i.row.sample.catalog_uuid for i in picks], picks[0].batch_id)
    for i in picks:
        w.cat.mark_licensed(i.row.sample.catalog_uuid)
    after = w.plan(balance=190)  # the 10 bought sounds cost 10 credits
    assert after.total == 190  # live balance - keep


def test_plan_is_deterministic_and_two_pass_kit_bonus(tmp_path):
    w = World(tmp_path)
    for p in range(1, 12):
        for g in ("kicks", "snares_claps", "hats_cymbals", "perc_oneshots"):
            w.add(g, pack=p, n=2)
    a = w.plan(balance=150)
    b = w.plan(balance=150)
    assert [i.row.sample.catalog_uuid for i in a.items] == [i.row.sample.catalog_uuid for i in b.items]
    assert any("kit_bonus" in i.why for i in a.items)  # packs in 3+ groups earn the bonus in pass 2
    assert [i.order for i in a.items] == list(range(1, len(a.items) + 1))


def test_floors_prefer_upper_scores_and_shortfall_is_filled_below(tmp_path):
    w = World(tmp_path, per_pack_cap=50, max_per_pack_per_group=50, max_per_pack_total=50, per_label_share=1.0)
    for p in range(1, 21):
        w.add("kicks", pack=p, n=1)
    plan = w.plan(balance=60)
    kicks = [i for i in plan.items if (i.group_id, i.genre_id) == ("kicks", GENRE)]
    cell = next(c for c in plan.capacity if (c.group_id, c.genre_id) == ("kicks", GENRE))
    assert len(kicks) >= cell.picked
    top = sorted((-r.score for r in kicks))
    assert len(kicks) > 0 and top == sorted(top)


def test_order_balances_groups_for_early_stop(tmp_path):
    w = World(tmp_path)
    for p in range(1, 80):
        for g in ("kicks", "drum_loops"):
            w.add(g, pack=p, n=3)
    plan = w.plan(balance=400)
    n = len(plan.items) // 4
    head = Counter(i.group_id for i in plan.items[:n])
    whole = by_group(plan)
    for g in head:
        assert abs(head[g] / n - whole[g] / len(plan.items)) < 0.1


def test_order_items_unit():
    class R:
        def __init__(self, h):
            self.sample = type("S", (), {"file_hash": h})()

    from splice_curator.selection import PlanItem

    items = [PlanItem(0, "b", "a", GENRE, R(f"a{i}"), 1, 1 - i / 10, {}) for i in range(4)] + \
            [PlanItem(0, "b", "b", GENRE, R("b0"), 1, 0.5, {})]
    ordered = order_items(items)
    assert [i.row.sample.file_hash for i in ordered] == ["a0", "a1", "b0", "a2", "a3"]
    assert [i.order for i in ordered] == [1, 2, 3, 4, 5]


def test_release_leaves_manual_batches_and_bought_sounds(tmp_path):
    w = World(tmp_path)
    w.add("kicks", pack=1, n=3)
    a, b, c = w.cat.rows_for_group("kicks", GENRE)
    w.cat.mark_shortlisted([a.sample.catalog_uuid], "kicks-tech_house-20261023-200420")
    w.cat.mark_shortlisted([b.sample.catalog_uuid, c.sample.catalog_uuid], "kicks-tech_house-20261024-100000-ab")
    w.cat.mark_licensed(c.sample.catalog_uuid)
    assert w.cat.release_pending(suffix=AUTOBUY_SUFFIX) == 1
    assert w.cat.pending_batches() == {"kicks-tech_house-20261023-200420": 1}
    assert w.cat.get_by_catalog_uuid(c.sample.catalog_uuid).shortlisted_batch.endswith("-ab")
    assert w.cat.release_pending(batch_id="kicks-tech_house-20261023-200420") == 1
    assert w.cat.pending_batches() == {}


def test_off_genre_packs_are_not_candidates(tmp_path):
    w = World(tmp_path)
    w.add("kicks", pack=1, n=3)
    w.add("kicks", pack=2, n=3, pack_genre="rnb")
    plan = w.plan(balance=100)
    assert plan.total > 0 and all(i.row.sample.pack_genre == "tech house" for i in plan.items)
