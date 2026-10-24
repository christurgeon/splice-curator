"""Free, pack-first discovery (spec 5.2): charts, best packs per genre, best sounds per pack.

Only read-only searches. Results are cached with timestamps, so a re-run
inside the TTL makes few requests.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from .budget import GROUPS, Genre, Group
from .catalog import Catalog
from .client import AuthRejected, SpliceClient, SpliceError
from .config import Config
from .models import ChartPack, Pack, Sample
from .organize import classify
from .quality import is_off_genre, pack_name_matches

HOUSE_WORDS = ("house", "techno", "disco", "groove")
GROUP_FOR = {
    ("One-Shots", "Kicks"): "kicks",
    ("One-Shots", "Snares & Claps"): "snares_claps",
    ("One-Shots", "Hats & Cymbals"): "hats_cymbals",
    ("One-Shots", "Percussion"): "perc_oneshots",
    ("One-Shots", "Bass"): "bass_oneshots",
    ("One-Shots", "Synth"): "synth_stabs",
    ("One-Shots", "Vocals"): "fx_vocal_oneshots",
    ("One-Shots", "FX"): "fx_vocal_oneshots",
    ("Loops", "Drums"): "drum_loops",
    ("Loops", "Percussion"): "perc_loops",
    ("Loops", "Bass"): "bass_loops",
    ("Loops", "Chords & Keys"): "chord_loops",
    ("Loops", "Vocals"): "vocal_loops",
}
HAT_WORDS = {"hat", "hats", "hh", "chh", "ohh", "oh"}
ALIASES = {
    "closed": {"closed", "chh", "closedhat", "ch"},
    "open": {"open", "ohh", "openhat", "oh"},
    "rim": {"rim", "rimshot", "rims"},
    "chop": {"chop", "chops"},
    "shout": {"shout", "shouts", "shot", "shots"},
    "bass": {"bass", "bassline"},
}
_TOKEN = re.compile(r"[a-z0-9]+")
CATEGORIES = ("oneshot", "loop")
PAGE_LIMIT = 100


@dataclass
class DiscoverReport:
    requests: Counter = field(default_factory=Counter)
    packs_per_genre: dict[str, int] = field(default_factory=dict)
    packs_fetched: int = 0
    packs_cached: int = 0
    samples_recorded: int = 0
    chart_ok: bool = True
    warnings: list[str] = field(default_factory=list)
    stopped_reason: str | None = None


def group_for(sample: Sample) -> Group | None:
    """The budget group a sound belongs to, from ``organize.classify``."""
    group_id = GROUP_FOR.get(classify(sample))
    return next((g for g in GROUPS if g.id == group_id), None) if group_id else None


def _tokens(sample: Sample) -> set[str]:
    words = set(_TOKEN.findall(sample.basename.lower().rsplit(".", 1)[0]))
    for label in sample.tag_labels:
        words.update(_TOKEN.findall(label))
    return words


def infer_subtype(sample: Sample, group: Group) -> str:
    """The group's subtype that name and tag words match; the first subtype if none does."""
    if not group.subtypes:
        return ""
    words = _tokens(sample)
    singular = words | {w[:-1] for w in words if w.endswith("s") and len(w) > 3}
    for subtype in group.subtypes:
        needed = subtype.split()
        if all(singular & ({w} | ALIASES.get(w, set()) | (HAT_WORDS if w == "hat" else set())) for w in needed):
            return subtype
    return group.subtypes[0]


def _published(pack: Pack) -> datetime | None:
    try:
        return datetime.fromisoformat((pack.published_at or "").replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _is_recent(pack: Pack, today: datetime, months: int) -> bool:
    published = _published(pack)
    return published is not None and published >= today - timedelta(days=30.44 * months)


def _off_genre(pack: Pack) -> bool:
    return is_off_genre(pack.genre)


def _listed(names: list[str], *values: str | None) -> bool:
    lowered = {n.lower() for n in names}
    return any(v and v.lower() in lowered for v in values)


class Discoverer:
    def __init__(
        self,
        client: SpliceClient,
        catalog: Catalog,
        config: Config,
        now: Callable[[], datetime] = datetime.now,
        log: Callable[[str], None] = lambda message: None,
    ) -> None:
        self.client, self.catalog, self.config, self.now, self.log = client, catalog, config, now, log
        self.report = DiscoverReport()

    def _count(self, kind: str) -> None:
        self.report.requests[kind] += 1

    def _fresh(self, fetched: datetime | None, days: int) -> bool:
        return fetched is not None and self.now() - fetched < timedelta(days=days)

    # -- charts ---------------------------------------------------------------

    def refresh_charts(self, force: bool = False) -> None:
        cfg, cat = self.config, self.catalog
        try:
            if force or not self._fresh(cat.chart_fetched_at("global"), cfg.chart_ttl_days):
                charted = self._global_packs_chart()
                cat.replace_chart_packs("global", charted, self.now())
                self.log(f"global packs chart: {len(charted)} packs")
            if force or not self._fresh(cat.chart_fetched_at(None), cfg.chart_ttl_days):
                self._count("labels_chart")
                labels = self.client.labels_chart(50)
                cat.replace_chart_labels(labels, self.now())
                self.log(f"labels chart: {len(labels)} labels")
            for label in self._house_labels():
                if label["permalink"] and (force or not self._fresh(cat.chart_fetched_at(label["permalink"]),
                                                                      cfg.chart_ttl_days)):
                    self._count("packs_chart")
                    charted = self.client.packs_chart(50, provider_permalink=label["permalink"])
                    cat.replace_chart_packs(label["permalink"], charted, self.now())
                    self.log(f"{label['name']} chart: {len(charted)} packs")
        except AuthRejected:
            raise
        except SpliceError as exc:
            self.report.chart_ok = False
            self.report.warnings.append(f"charts unavailable, trending signals disabled: {exc}")

    def _global_packs_chart(self) -> list[ChartPack]:
        self._count("packs_chart")
        try:
            charted = self.client.packs_chart(100)
            if charted:
                return charted
        except AuthRejected:
            raise
        except SpliceError:
            pass  # the site uses perPage 50; fall back to it
        self._count("packs_chart")
        return self.client.packs_chart(50)

    def _house_labels(self) -> list[dict]:
        rows = [r for r in self.catalog.chart_label_rows()
                if any(w in t.lower() for t in _json_list(r["tags"]) for w in HOUSE_WORDS)]
        return [{"permalink": r["permalink"], "name": r["name"]} for r in rows[: self.config.chart_labels]]

    # -- packs ----------------------------------------------------------------

    def find_packs(self, genre: Genre) -> list[Pack]:
        """Popularity-sorted pack search for each prefix; store ranks; return the kept packs."""
        cfg = self.config
        today = self.now()
        best_all: dict[str, int] = {}
        best_recent: dict[str, int] = {}
        found: dict[str, Pack] = {}
        for prefix in genre.prefixes:
            rank = recent_rank = 0
            for page in range(1, cfg.pack_pages + 1):
                self._count("pack_search")
                result = self.client.pack_search(prefix, page, limit=50)
                for pack in result.packs:
                    rank += 1
                    if _off_genre(pack) or _listed(cfg.exclude_packs, pack.name) \
                            or _listed(cfg.exclude_labels, pack.label) \
                            or pack_name_matches(pack.name, cfg.exclude_pack_patterns):
                        continue
                    found.setdefault(pack.uuid, pack)
                    best_all[pack.uuid] = min(best_all.get(pack.uuid, rank), rank)
                    if _is_recent(pack, today, cfg.recent_months):
                        recent_rank += 1
                        best_recent[pack.uuid] = min(best_recent.get(pack.uuid, recent_rank), recent_rank)
                if page >= result.total_pages:
                    break
        charted = {r["pack_uuid"] for r in self.catalog.chart_pack_rows()}

        def strength(uuid: str) -> float:
            all_pop = 1 / (1 + best_all[uuid] / 25)
            recent_pop = 1 / (1 + best_recent[uuid] / 25) if uuid in best_recent else 0.0
            return max(all_pop, recent_pop)

        ordered = sorted(found, key=lambda u: (-strength(u), best_all[u], u))
        kept = ordered[: cfg.packs_per_genre] + [u for u in ordered[cfg.packs_per_genre:] if u in charted]
        self.catalog.upsert_packs([found[u] for u in kept])
        self.catalog.clear_pack_ranks(genre.id)
        for u in kept:
            self.catalog.set_pack_rank(u, genre.id, best_all[u], best_recent.get(u))
        self.report.packs_per_genre[genre.id] = len(kept)
        self.log(f"{genre.id}: kept {len(kept)} of {len(found)} packs")
        return [found[u] for u in kept]

    # -- sounds ---------------------------------------------------------------

    def fetch_pack_samples(self, pack: Pack, force: bool = False) -> None:
        if not force and self._fresh(self.catalog.pack_fetched_at(pack.uuid), self.config.discovery_ttl_days):
            self.report.packs_cached += 1
            return
        for category in CATEGORIES:
            page = 1
            while True:
                self._count("pack_samples")
                result = self.client.pack_samples(pack.uuid, category, page, limit=PAGE_LIMIT)
                self.catalog.record_pack_samples(
                    result.samples, pack.uuid, (page - 1) * PAGE_LIMIT, result.total_records)
                self.report.samples_recorded += len(result.samples)
                if not result.samples or page >= min(result.total_pages, 2):
                    break
                page += 1
        self.catalog.mark_pack_fetched(pack.uuid, self.now())
        self.report.packs_fetched += 1

    def assign_pack(self, pack: Pack, genre: Genre) -> int:
        n = 0
        for row in self.catalog.rows_for_pack(pack.uuid):
            group = group_for(row.sample)
            if group is None or row.sample.category != group.category:
                continue
            self.catalog.assign(row.sample.file_hash, group.id, genre.id, infer_subtype(row.sample, group))
            n += 1
        return n


def _json_list(text: str | None) -> list[str]:
    import json

    try:
        return [str(v) for v in json.loads(text or "[]")]
    except ValueError:
        return []


def run_discover(
    client: SpliceClient,
    catalog: Catalog,
    config: Config,
    genres: list[Genre] | None = None,
    force: bool = False,
    now: Callable[[], datetime] = datetime.now,
    log: Callable[[str], None] = lambda message: None,
) -> DiscoverReport:
    d = Discoverer(client, catalog, config, now, log)
    try:
        d.refresh_charts(force)
        by_genre = {g.id: (g, d.find_packs(g)) for g in (genres or config.genres)}
        done: set[str] = set()
        for genre, packs in by_genre.values():
            for pack in packs:
                if pack.uuid not in done:
                    done.add(pack.uuid)
                    d.fetch_pack_samples(pack, force)
                    log(f"{pack.name}: {len(done)} packs done")
                d.assign_pack(pack, genre)
    except (AuthRejected, SpliceError) as exc:
        d.report.stopped_reason = str(exc)
    return d.report
