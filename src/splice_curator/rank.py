"""Quality ranking from Splice metadata (spec 7.4)."""

from __future__ import annotations

from collections import Counter

from .budget import TARGET_GENRES, Group
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
