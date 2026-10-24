"""Signal-based score for autobuy picks (spec 5.1). Pure given a ``QualityContext``.

A missing signal scores 0. Every score comes with its signal values (``why``)
so the owner can see why a sound was chosen.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .catalog import UNRANKED, Catalog, CatalogRow
from .config import Config
from .models import Sample

OFF_GENRES = frozenset({
    "trap", "hip hop", "hip-hop", "dubstep", "drum and bass", "drum & bass", "drum n bass", "dnb",
    "future bass", "lo-fi", "lofi", "pop", "rock", "metal", "country", "reggaeton", "r&b", "edm trap",
})
SONIC_TAGS = frozenset({"groovy", "punchy", "dry", "swing", "shuffle", "tight"})
LOOP_JUNK = frozenset({"riser", "build", "buildup", "fill", "fills", "reverse", "reversed"})
BAD_WORDS = frozenset({"808", "trap"})
TOP_WORDS = frozenset({"top", "tops"})
FULL_WORDS = frozenset({"full", "fill", "fills"})
LOOP_BARS = (1, 2, 4, 8, 16)
CHART_SIZE = 50
FRESH_MONTHS = 24
OWNER_MIN = 3
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class QualityContext:
    weights: dict[str, float]
    today: datetime
    pack_ranks: dict[tuple[str, str], tuple[int, int | None]] = field(default_factory=dict)
    chart_packs: dict[str, list[tuple[int, int | None]]] = field(default_factory=dict)
    chart_labels: dict[str, tuple[int, int | None]] = field(default_factory=dict)
    pack_published: dict[str, datetime] = field(default_factory=dict)
    pack_tags: dict[str, set[str]] = field(default_factory=dict)
    pack_by_name: dict[tuple[str, str], str] = field(default_factory=dict)
    owned_packs: Counter = field(default_factory=Counter)
    owned_labels: Counter = field(default_factory=Counter)
    prefer_packs: frozenset[str] = frozenset()
    prefer_labels: frozenset[str] = frozenset()
    exclude_packs: frozenset[str] = frozenset()
    exclude_labels: frozenset[str] = frozenset()

    def pack_uuid(self, sample: Sample) -> str | None:
        if sample.pack_uuid:
            return sample.pack_uuid
        return self.pack_by_name.get(((sample.pack or "").lower(), (sample.label or "").lower()))


@dataclass(frozen=True)
class Scored:
    row: CatalogRow
    score: float
    why: dict[str, float]


def _lower_set(values: list[str]) -> frozenset[str]:
    return frozenset(v.lower() for v in values)


def _parse_time(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def build_context(catalog: Catalog, config: Config, today: datetime | None = None) -> QualityContext:
    """Read packs, ranks, charts, and owned counts from the catalog."""
    ctx = QualityContext(
        weights=dict(config.scoring), today=today or datetime.now(),
        prefer_packs=_lower_set(config.prefer_packs), prefer_labels=_lower_set(config.prefer_labels),
        exclude_packs=_lower_set(config.exclude_packs), exclude_labels=_lower_set(config.exclude_labels),
    )
    for r in catalog.pack_rows():
        ctx.pack_by_name[((r["name"] or "").lower(), (r["label"] or "").lower())] = r["uuid"]
        published = _parse_time(r["published_at"])
        if published:
            ctx.pack_published[r["uuid"]] = published
        ctx.pack_tags[r["uuid"]] = {str(t).lower() for t in json.loads(r["tags"] or "[]")}
    for r in catalog.pack_rank_rows():
        ctx.pack_ranks[(r["pack_uuid"], r["genre"])] = (r["all_time_rank"], r["recent_rank"])
    for r in catalog.chart_pack_rows():
        ctx.chart_packs.setdefault(r["pack_uuid"], []).append((r["position"], r["last_position"]))
    for r in catalog.chart_label_rows():
        ctx.chart_labels[(r["name"] or "").lower()] = (r["position"], r["last_position"])
    for row in catalog.licensed_rows():
        if uuid := ctx.pack_uuid(row.sample):
            ctx.owned_packs[uuid] += 1
        if row.sample.label:
            ctx.owned_labels[row.sample.label.lower()] += 1
    return ctx


def _tokens(sample: Sample) -> set[str]:
    words = set(_TOKEN.findall(sample.basename.lower().rsplit(".", 1)[0]))
    for label in sample.tag_labels:
        words.update(_TOKEN.findall(label))
    return words


def _text(sample: Sample) -> str:
    return " ".join(_TOKEN.findall(f"{sample.basename} {' '.join(sample.tag_labels)}".lower()))


def is_excluded(sample: Sample, ctx: QualityContext) -> bool:
    return (sample.pack or "").lower() in ctx.exclude_packs or (sample.label or "").lower() in ctx.exclude_labels


def loop_shape_ok(sample: Sample) -> bool:
    """A loop must last about 1, 2, 4, 8, or 16 bars (within 5%) at its BPM."""
    if not sample.bpm or not sample.duration_ms:
        return False
    bars = sample.duration_ms / 1000 * sample.bpm / 240
    return any(abs(bars - n) <= 0.05 * n for n in LOOP_BARS)


def _rank_pop(rank: int | None, scale: float) -> float:
    return 0.0 if rank is None else 1 / (1 + rank / scale)


def _chart_value(pos: int, last: int | None, rising_bonus: float) -> float:
    value = max(0.0, 1 - (pos - 1) / CHART_SIZE)
    if value > 0 and (last is None or last > pos):
        value += rising_bonus
    return value


def signals(row: CatalogRow, group_id: str, genre_id: str, ctx: QualityContext, kit: bool = False) -> dict[str, float]:
    """Raw signal values (weight 1) plus penalties as negative numbers."""
    s, w = row.sample, ctx.weights
    pack = ctx.pack_uuid(s)
    out: dict[str, float] = {}
    if row.in_pack_position is not None and row.in_pack_count:
        out["in_pack_pop"] = max(0.0, 1 - row.in_pack_position / row.in_pack_count)
    if row.best_position < UNRANKED:
        out["query_pop"] = 1 / (1 + row.best_position / 50)
    all_rank, recent_rank = ctx.pack_ranks.get((pack, genre_id), (None, None)) if pack else (None, None)
    out["pack_pop"] = _rank_pop(all_rank, 25)
    out["pack_recent_pop"] = _rank_pop(recent_rank, 25)

    charts = list(ctx.chart_packs.get(pack, [])) if pack else []
    trending = max((_chart_value(p, last, w["trending_rising_bonus"]) for p, last in charts), default=0.0)
    out["trending"] = trending
    label_pos = ctx.chart_labels.get((s.label or "").lower())
    out["label_chart"] = _chart_value(label_pos[0], None, 0.0) if label_pos else 0.0

    published = ctx.pack_published.get(pack) if pack else None
    if published:
        months = max(0.0, (ctx.today - published).days / 30.44)
        out["fresh"] = max(0.0, 1 - months / FRESH_MONTHS)
    pack_tags = ctx.pack_tags.get(pack, set()) if pack else set()
    out["sonic_bonus"] = 1.0 if (s.tag_labels | pack_tags) & SONIC_TAGS else 0.0

    off_genre = (s.pack_genre or "").lower() in OFF_GENRES
    if not off_genre:
        if (pack and ctx.owned_packs[pack] >= OWNER_MIN):
            out["owner_affinity"] = 1.0
        elif s.label and ctx.owned_labels[s.label.lower()] >= OWNER_MIN:
            out["owner_affinity"] = 0.5
    out["kit_bonus"] = 1.0 if kit else 0.0
    if (s.pack or "").lower() in ctx.prefer_packs or (s.label or "").lower() in ctx.prefer_labels:
        out["prefer_bonus"] = 1.0
    return out | _penalties(s, group_id, off_genre)


def _penalties(s: Sample, group_id: str, off_genre: bool) -> dict[str, float]:
    out: dict[str, float] = {}
    words, text = _tokens(s), _text(s)
    if off_genre:
        out["off_genre_penalty"] = -1.0
    if words & BAD_WORDS or "hip hop" in text:
        out["bad_word_penalty"] = -1.0
    if s.category == "loop" and words & LOOP_JUNK:
        out["loop_junk_penalty"] = -1.0
    if group_id == "drum_loops":
        if words & FULL_WORDS:
            out["full_loop_penalty"] = -1.0
        if words & TOP_WORDS or "no kick" in text or "nokick" in words:
            out["tops_bonus"] = 1.0
    return out


def score_row(row: CatalogRow, group_id: str, genre_id: str, ctx: QualityContext, kit: bool = False) -> Scored:
    values = signals(row, group_id, genre_id, ctx, kit)
    why = {name: round(value * ctx.weights.get(name, 0.0), 4) for name, value in values.items() if value}
    return Scored(row, round(sum(why.values()), 6), why)


def format_why(why: dict[str, float]) -> str:
    """Compact text for the CSV and review page, strongest signals first."""
    parts = sorted(why.items(), key=lambda kv: -abs(kv[1]))
    return " ".join(f"{name}={value:+.2f}" for name, value in parts)


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (0-100) of ``values``; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)
