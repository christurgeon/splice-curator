"""Quality ranking from Splice metadata (spec 7.4)."""

from __future__ import annotations

import math
import re
from collections import Counter

from .budget import TARGET_GENRES, Genre, Group, subtype_quotas
from .catalog import CatalogRow
from .models import Sample

TOP_POSITION = 200


def top_labels(rows: list[CatalogRow], n: int = 20) -> set[str]:
    """The ``n`` most frequent labels among rows found in the top results."""
    counts = Counter(r.sample.label for r in rows if r.sample.label and r.best_position < TOP_POSITION)
    return {label for label, _ in counts.most_common(n)}


def score(row: CatalogRow, group: Group, labels: set[str]) -> float:
    s = row.sample
    tags = s.tag_labels
    value = 1 / (1 + row.best_position / 50)
    if tags & TARGET_GENRES or (s.pack_genre or "").lower() in TARGET_GENRES:
        value += 0.2
    if s.label in labels:
        value += 0.1
    if group.want_tags & tags:
        value += 0.1
    return value


def _pack_key(sample: Sample) -> str:
    return sample.pack or sample.name.split("/", 1)[0]


def build_shortlist(rows: list[CatalogRow], group: Group, size: int, per_pack_cap: int) -> list[Sample]:
    labels = top_labels(rows)
    candidates = [
        r for r in rows
        if not r.sample.licensed
        and r.shortlisted_batch is None
        and r.sample.category == group.category
    ]
    candidates.sort(key=lambda r: (-score(r, group, labels), r.best_position, r.sample.file_hash))
    picked: list[Sample] = []
    per_pack: Counter[str] = Counter()
    for r in candidates:
        if len(picked) >= size:
            break
        pack = _pack_key(r.sample)
        if per_pack[pack] >= per_pack_cap:
            continue
        per_pack[pack] += 1
        picked.append(r.sample)
    return picked


_DIGITS = re.compile(r"\d+")
_SEPARATORS = re.compile(r"[_\-\s.]+")
KEY_CAPPED_GROUPS = frozenset({"bass_loops", "chord_loops"})


def near_dup_key(sample: Sample) -> tuple[str, str]:
    """Same pack and same name apart from digits and separators means a near-duplicate."""
    stem = sample.basename.lower().rsplit(".", 1)[0]
    stem = _SEPARATORS.sub(" ", _DIGITS.sub("", stem)).strip()
    return _pack_key(sample), stem


def _genre_match(sample: Sample, genre: Genre) -> bool:
    return bool(genre.tags & sample.tag_labels) or (sample.pack_genre or "").lower() in genre.tags


class _Variety:
    """Tracks the per-pack, per-label, near-duplicate, and key limits for one shortlist."""

    def __init__(self, group: Group, size: int, per_pack_cap: int, per_label_cap: int, key_share_cap: float):
        self.per_pack_cap, self.per_label_cap = per_pack_cap, per_label_cap
        self.key_cap = math.ceil(key_share_cap * size) if group.id in KEY_CAPPED_GROUPS else None
        self.packs: Counter[str] = Counter()
        self.labels: Counter[str] = Counter()
        self.keys: Counter[tuple[str, str | None]] = Counter()
        self.stems: set[tuple[str, str]] = set()

    def take(self, s: Sample) -> bool:
        """Accept ``s`` if every limit allows it, and record it."""
        pack, dup = _pack_key(s), near_dup_key(s)
        key = (s.key, s.chord_type)
        if self.packs[pack] >= self.per_pack_cap or dup in self.stems:
            return False
        if s.label and self.labels[s.label] >= self.per_label_cap:
            return False
        if self.key_cap is not None and s.key and self.keys[key] >= self.key_cap:
            return False
        self.packs[pack] += 1
        self.stems.add(dup)
        if s.label:
            self.labels[s.label] += 1
        if s.key:
            self.keys[key] += 1
        return True


def build_genre_shortlist(
    rows: list[CatalogRow],
    group: Group,
    genre: Genre,
    size: int,
    per_pack_cap: int,
    per_label_cap: int = 30,
    key_share_cap: float = 0.15,
) -> list[Sample]:
    """Pick ``size`` sounds for one genre, spread evenly over the group's subtypes."""
    labels = top_labels(rows)
    candidates = [
        r for r in rows
        if not r.sample.licensed
        and r.shortlisted_batch is None
        and r.sample.category == group.category
        and _genre_match(r.sample, genre)
        and (group.category != "loop" or (r.sample.bpm is not None and genre.bpm[0] <= r.sample.bpm <= genre.bpm[1]))
    ]
    candidates.sort(key=lambda r: (-score(r, group, labels), r.best_position, r.sample.file_hash))
    by_subtype: dict[str, list[Sample]] = {st: [] for st in group.subtypes}
    for r in candidates:
        found = {st for g, st in r.genres if g == genre.id}
        first = next((st for st in group.subtypes if st in found), None)
        if first:
            by_subtype[first].append(r.sample)
    variety = _Variety(group, size, per_pack_cap, per_label_cap, key_share_cap)
    picked: list[Sample] = []
    chosen: set[str] = set()

    def fill(pool: list[Sample], want: int) -> int:
        n = 0
        for s in pool:
            if n >= want:
                break
            if s.file_hash not in chosen and variety.take(s):
                chosen.add(s.file_hash)
                picked.append(s)
                n += 1
        return n

    carry = 0
    for subtype, quota in subtype_quotas(group, size).items():
        got = fill(by_subtype[subtype], quota + carry)
        carry = quota + carry - got  # unused quota goes to the later subtypes
    if len(picked) < size:
        fill([r.sample for r in candidates], size - len(picked))
    return picked
