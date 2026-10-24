from datetime import datetime

import pytest

from splice_curator.budget import get_group
from splice_curator.catalog import UNRANKED, Catalog
from splice_curator.client import AuthRejected, SpliceError
from splice_curator.config import load_config
from splice_curator.discover import group_for, infer_subtype, run_discover
from splice_curator.models import ChartLabel, ChartPack, Pack, PackPage, SearchPage
from tests.helpers import make_sample

NOW = datetime(2026, 10, 24, 12, 0)


def puuid(i):
    return f"aaaaaaaa-0000-4000-8000-{i:012x}"


def make_pack(i, genre="tech house", published="2026-08-01T00:00:00Z", label="Label A", name=None):
    return Pack(puuid(i), name or f"Pack {i}", genre, f"pack-{i}", label, published, [], 50)


class FakeDiscoveryClient:
    def __init__(self, packs_by_query=None, samples_by_pack=None):
        self.packs_by_query = packs_by_query or {}
        self.samples_by_pack = samples_by_pack or {}
        self.calls = []
        self.chart_error = None

    def pack_search(self, query, page, limit=50):
        self.calls.append(("pack_search", query, page))
        packs = self.packs_by_query.get(query, [])
        chunk = packs[(page - 1) * limit : page * limit]
        return PackPage(len(packs), max(1, -(-len(packs) // limit)), chunk)

    def pack_samples(self, pack_uuid, category, page, limit=100):
        self.calls.append(("pack_samples", pack_uuid, category, page))
        items = [s for s in self.samples_by_pack.get(pack_uuid, []) if s.category == category]
        chunk = items[(page - 1) * limit : page * limit]
        return SearchPage(len(items), max(1, -(-len(items) // limit)), chunk)

    def packs_chart(self, per_page=50, provider_permalink=None):
        self.calls.append(("packs_chart", per_page, provider_permalink))
        if self.chart_error:
            raise self.chart_error
        scope = provider_permalink or "global"
        return [ChartPack(puuid(1), "Pack 1", "Label A", 4, None, scope)]

    def labels_chart(self, per_page=50):
        self.calls.append(("labels_chart", per_page))
        if self.chart_error:
            raise self.chart_error
        return [ChartLabel("l1", "Label A", "label-a", 1, 2, 30, ["tech house"]),
                ChartLabel("l2", "Label B", "label-b", 2, 2, 30, ["trap"])]

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


def pack_sample(i, pack, name, category="oneshot", tags=(("kicks", None),), **kw):
    return make_sample(i, name=f"{pack.name}/{name}", pack=pack.name, label=pack.label, category=category,
                       tags=list(tags), pack_uuid=pack.uuid, **kw)


@pytest.fixture
def cfg(tmp_path):
    return load_config(path=None, overrides={"data_dir": tmp_path / "d", "packs_per_genre": 2,
                                             "genres": {"minimal": {"weight": 0}, "deep_house": {"weight": 0},
                                                        "afro_house": {"weight": 0},
                                                        "classic_house": {"weight": 0},
                                                        "tech_house": {"weight": 1.0}}})


def setup(tmp_path):
    packs = [make_pack(i) for i in range(1, 5)]
    packs.insert(1, make_pack(9, genre="trap"))  # off-genre, dropped
    samples = {
        packs[0].uuid: [pack_sample(1, packs[0], "Kick_A.wav"),
                        pack_sample(2, packs[0], "Loop_A.wav", category="loop",
                                    tags=(("drums", None),), bpm=126, duration_ms=3810)],
        packs[2].uuid: [pack_sample(3, packs[2], "Clap_A.wav", tags=(("claps", None),))],
    }
    client = FakeDiscoveryClient({"tech house": packs}, samples)
    return client, Catalog(tmp_path / "c.sqlite"), packs


def test_discover_stores_ranks_samples_and_group_genre(tmp_path, cfg):
    client, cat, packs = setup(tmp_path)
    report = run_discover(client, cat, cfg, now=lambda: NOW)
    assert report.packs_per_genre == {"tech_house": 2}
    ranks = {r["pack_uuid"]: (r["all_time_rank"], r["recent_rank"]) for r in cat.pack_rank_rows()}
    assert ranks[packs[0].uuid] == (1, 1)
    assert puuid(9) not in ranks  # trap dropped
    (kick,) = cat.rows_for_group("kicks", "tech_house")
    assert kick.sample.catalog_uuid.endswith("01") and ("tech_house", "kick") in kick.genres
    assert kick.best_position == UNRANKED and kick.in_pack_position == 0
    (loop,) = cat.rows_for_group("drum_loops", "tech_house")
    assert loop.in_pack_count == 1


def test_charts_fetched_for_house_labels_only(tmp_path, cfg):
    client, cat, _ = setup(tmp_path)
    run_discover(client, cat, cfg, now=lambda: NOW)
    chart_calls = [c for c in client.calls if c[0] == "packs_chart"]
    assert ("packs_chart", 100, None) in chart_calls
    assert ("packs_chart", 50, "label-a") in chart_calls
    assert not any(c[2] == "label-b" for c in chart_calls)  # trap label skipped
    assert cat.chart_fetched_at("global") == NOW


def test_ttl_caching_avoids_requests(tmp_path, cfg):
    client, cat, _ = setup(tmp_path)
    first = run_discover(client, cat, cfg, now=lambda: NOW)
    n_first = len(client.calls)
    assert first.requests["pack_samples"] > 0
    client.calls.clear()
    second = run_discover(client, cat, cfg, now=lambda: NOW)
    assert client.count("pack_samples") == 0 and client.count("packs_chart") == 0
    assert client.count("labels_chart") == 0
    assert client.count("pack_search") > 0  # pack search always reruns
    assert second.packs_cached == second.packs_per_genre["tech_house"] and n_first > len(client.calls)
    client.calls.clear()
    later = datetime(2026, 11, 2, 12, 0)
    run_discover(client, cat, cfg, now=lambda: later)
    assert client.count("pack_samples") > 0 and client.count("labels_chart") == 1


def test_chart_failure_disables_trending_with_warning(tmp_path, cfg):
    client, cat, _ = setup(tmp_path)
    client.chart_error = SpliceError("GraphQL error: nope")
    report = run_discover(client, cat, cfg, now=lambda: NOW)
    assert report.chart_ok is False and "trending" in report.warnings[0]
    assert report.samples_recorded > 0  # discovery continues


def test_auth_failure_stops_run_and_reports(tmp_path, cfg):
    client, cat, _ = setup(tmp_path)

    def boom(*a, **k):
        raise AuthRejected("log in to splice.com again")

    client.pack_search = boom
    report = run_discover(client, cat, cfg, now=lambda: NOW)
    assert "log in" in report.stopped_reason


def test_recent_rank_only_for_recent_packs(tmp_path, cfg):
    packs = [make_pack(1, published="2020-01-01T00:00:00Z"), make_pack(2)]
    client = FakeDiscoveryClient({"tech house": packs})
    cat = Catalog(tmp_path / "c.sqlite")
    run_discover(client, cat, cfg, now=lambda: NOW)
    ranks = {r["pack_uuid"]: (r["all_time_rank"], r["recent_rank"]) for r in cat.pack_rank_rows()}
    assert ranks[puuid(1)] == (1, None) and ranks[puuid(2)] == (2, 1)


def test_excluded_packs_and_labels_dropped(tmp_path, cfg):
    cfg.exclude_packs = ["pack 1"]
    cfg.exclude_labels = ["Label X"]
    packs = [make_pack(1), make_pack(2, label="Label X"), make_pack(3)]
    cat = Catalog(tmp_path / "c.sqlite")
    run_discover(FakeDiscoveryClient({"tech house": packs}), cat, cfg, now=lambda: NOW)
    assert {r["pack_uuid"] for r in cat.pack_rank_rows()} == {puuid(3)}


def test_second_page_fetched_for_big_packs(tmp_path, cfg):
    pack = make_pack(1)
    kicks = [pack_sample(i, pack, f"Kick_{i}.wav") for i in range(1, 131)]
    client = FakeDiscoveryClient({"tech house": [pack]}, {pack.uuid: kicks})
    cat = Catalog(tmp_path / "c.sqlite")
    run_discover(client, cat, cfg, now=lambda: NOW)
    assert [c[3] for c in client.calls if c[0] == "pack_samples" and c[2] == "oneshot"] == [1, 2]
    rows = cat.rows_for_group("kicks", "tech_house")
    assert len(rows) == 130
    assert sorted(r.in_pack_position for r in rows) == list(range(130))


def test_group_and_subtype_inference():
    kick = make_sample(1, tags=[("kicks", None)])
    assert group_for(kick).id == "kicks"
    hat = make_sample(2, name="P/FSS_Open_Hat_01.wav", tags=[("hats", None)])
    assert group_for(hat).id == "hats_cymbals" and infer_subtype(hat, get_group("hats_cymbals")) == "open hat"
    closed = make_sample(3, name="P/Closed_HH_02.wav", tags=[("hats", None)])
    assert infer_subtype(closed, get_group("hats_cymbals")) == "closed hat"
    ride = make_sample(4, name="P/Ride_01.wav", tags=[("cymbals", None)])
    assert infer_subtype(ride, get_group("hats_cymbals")) == "ride"
    clap = make_sample(5, name="P/Big_Clap.wav", tags=[("claps", None)])
    assert infer_subtype(clap, get_group("snares_claps")) == "clap"
    snare = make_sample(6, name="P/Tight_Snare.wav", tags=[("snares", None)])
    assert infer_subtype(snare, get_group("snares_claps")) == "snare"
    conga = make_sample(7, name="P/Perc_01.wav", tags=[("percussion", None), ("congas", None)])
    assert infer_subtype(conga, get_group("perc_oneshots")) == "conga"
    odd = make_sample(8, name="P/Thing.wav", tags=[("hats", None)])
    assert infer_subtype(odd, get_group("hats_cymbals")) == "closed hat"  # fallback: first subtype
    other = make_sample(9, name="P/X.wav", tags=[("misc", None)])
    assert group_for(other) is None
    loop = make_sample(10, category="loop", tags=[("drums", None)], bpm=126)
    assert group_for(loop).id == "drum_loops"


def test_pack_pages_limits_pack_search(tmp_path, cfg):
    packs = [make_pack(i) for i in range(1, 121)]
    client = FakeDiscoveryClient({"tech house": packs})
    cfg.pack_pages = 2
    run_discover(client, Catalog(tmp_path / "c.sqlite"), cfg, now=lambda: NOW)
    assert [c[2] for c in client.calls if c[0] == "pack_search"] == [1, 2]
