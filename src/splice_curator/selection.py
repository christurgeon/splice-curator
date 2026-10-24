"""Pick sounds for the autobuy plan (spec 5.3). Pure: reads the catalog, never writes it.

Two passes per group and genre (the second adds the kit bonus), percentile
floors, variety caps, then a final pass that places the credits that are
left. The result is deterministic for a given catalog.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .budget import GROUPS, RESERVE, Genre, Group, _split, genre_quotas, subtype_quotas
from .catalog import UNRANKED, Catalog, CatalogRow
from .config import Config
from .quality import QualityContext, is_off_genre, Scored, is_excluded, loop_shape_ok, percentile, score_row
from .rank import KEY_CAPPED_GROUPS, _genre_match, _pack_key, dup_key

REJECTED_PREFIX = "rejected-"  # tag of sounds dropped with `release --rejected` (cli.cmd_release)
AUTOBUY_SUFFIX = "-ab"  # batch ids of autobuy plans end with this; `release` and `--replace` use it
SAMPLE_PRICE = 1


@dataclass
class PlanItem:
    order: int
    batch_id: str
    group_id: str
    genre_id: str
    row: CatalogRow
    price: int
    score: float
    why: dict[str, float]
    final_pass: bool = False


@dataclass
class CapacityRow:
    group_id: str
    genre_id: str
    candidates: int  # sounds that pass every filter
    quota: int  # credits still to place in this cell
    picked: int  # placed by the group stage
    bought: int
    pending: int

    @property
    def shortfall(self) -> int:
        return max(0, self.quota - self.picked)


@dataclass
class Plan:
    items: list[PlanItem]
    balance: int
    keep: int
    spendable: int  # balance - keep
    pending: int  # credits already marked in unbought batches
    capacity: list[CapacityRow]
    unplaced: int = 0
    unplaced_reason: str = ""
    created: datetime = field(default_factory=datetime.now)

    @property
    def total(self) -> int:
        return sum(i.price for i in self.items)

    @property
    def batches(self) -> dict[str, list[PlanItem]]:
        out: dict[str, list[PlanItem]] = {}
        for item in self.items:
            out.setdefault(item.batch_id, []).append(item)
        return out


def batch_id_for(group_id: str, genre_id: str, when: datetime) -> str:
    return f"{group_id}-{genre_id}-{when:%Y%m%d-%H%M%S}{AUTOBUY_SUFFIX}"


def _sort_key(c: Scored):
    r = c.row
    return (-c.score, r.in_pack_position if r.in_pack_position is not None else UNRANKED,
            r.best_position, r.sample.file_hash)


def _eligible(row: CatalogRow, group: Group, genre: Genre, ctx: QualityContext, owned_keys: set) -> bool:
    s = row.sample
    if s.licensed or row.shortlisted_batch is not None or s.category != group.category:
        return False
    if is_off_genre(s.pack_genre) or is_excluded(s, ctx) or (s.basename.lower(), s.duration_ms) in owned_keys:
        return False
    if row.in_pack_position is None and not _genre_match(s, genre):
        return False
    if group.category == "loop":
        return s.bpm is not None and genre.bpm[0] <= s.bpm <= genre.bpm[1] and loop_shape_ok(s)
    return True


class _State:
    """Counters for the variety caps of one selection run."""

    def __init__(self, config: Config, targets: dict[str, int]) -> None:
        self.cfg = config
        self.label_cap = {g: max(1, math.ceil(config.per_label_share * t)) for g, t in targets.items()}
        self.key_cap = {g: max(1, math.ceil(config.key_share_cap * t)) for g, t in targets.items()}
        self.chosen: set[str] = set()
        self.dups: set[tuple[str, str]] = set()
        self.cross: set[tuple[str, int | None]] = set()
        self.pack_total: Counter = Counter()
        self.pack_group: Counter = Counter()
        self.pack_batch: Counter = Counter()
        self.label_group: Counter = Counter()
        self.key_group: Counter = Counter()

    def reason_blocked(self, c: Scored, group: Group, genre: Genre) -> str | None:
        """None if ``c`` may be taken, else why not."""
        s, cfg = c.row.sample, self.cfg
        pack = _pack_key(s)
        if s.file_hash in self.chosen:
            return "chosen"
        if dup_key(s) in self.dups or (s.basename.lower(), s.duration_ms) in self.cross:
            return "duplicate"
        if self.pack_batch[(group.id, genre.id, pack)] >= cfg.per_pack_cap:
            return "caps"
        if self.pack_group[(group.id, pack)] >= cfg.max_per_pack_per_group or \
                self.pack_total[pack] >= cfg.max_per_pack_total:
            return "caps"
        if s.label and self.label_group[(group.id, s.label)] >= self.label_cap[group.id]:
            return "caps"
        if group.id in KEY_CAPPED_GROUPS and s.key and \
                self.key_group[(group.id, s.key, s.chord_type)] >= self.key_cap[group.id]:
            return "caps"
        return None

    def take(self, c: Scored, group: Group, genre: Genre) -> bool:
        if self.reason_blocked(c, group, genre):
            return False
        s = c.row.sample
        pack = _pack_key(s)
        self.chosen.add(s.file_hash)
        self.dups.add(dup_key(s))
        self.cross.add((s.basename.lower(), s.duration_ms))
        self.pack_batch[(group.id, genre.id, pack)] += 1
        self.pack_group[(group.id, pack)] += 1
        self.pack_total[pack] += 1
        if s.label:
            self.label_group[(group.id, s.label)] += 1
        if group.id in KEY_CAPPED_GROUPS and s.key:
            self.key_group[(group.id, s.key, s.chord_type)] += 1
        return True


def _fill_cell(pool: list[Scored], quota: int, group: Group, genre: Genre, state: _State) -> list[Scored]:
    """Fill a cell: best first above the high floor, then above the low floor; subtypes stay balanced."""
    ordered = sorted(pool, key=_sort_key)
    scores = [c.score for c in ordered]
    floors = (percentile(scores, state.cfg.floor_high), percentile(scores, state.cfg.floor_low))
    picked: list[Scored] = []

    def fill(candidates: list[Scored], want: int) -> int:
        n = 0
        for c in candidates:
            if n >= want:
                break
            if state.take(c, group, genre):
                picked.append(c)
                n += 1
        return n

    for floor in floors:
        left = quota - len(picked)
        if left <= 0:
            break
        above = [c for c in ordered if c.score >= floor and c.row.sample.file_hash not in state.chosen]
        by_subtype: dict[str, list[Scored]] = {st: [] for st in group.subtypes}
        for c in above:
            found = {st for g, st in c.row.genres if g == genre.id}
            first = next((st for st in group.subtypes if st in found), None)
            if first:
                by_subtype[first].append(c)
        carry = 0
        for subtype, share in subtype_quotas(group, left).items():
            got = fill(by_subtype[subtype], share + carry)
            carry = share + carry - got
        if len(picked) < quota:
            fill(above, quota - len(picked))
    return picked


def _targets(config: Config, spendable: int, bought_total: int) -> dict[str, int]:
    """Group credit targets that scale the approved table to the live balance, minus the reserve share."""
    base = sum(g.credits for g in GROUPS)
    group_total = round((spendable + bought_total) * base / (base + RESERVE))
    return dict(zip((g.id for g in GROUPS), _split(group_total, [g.credits / base for g in GROUPS])))


def make_plan(
    catalog: Catalog, config: Config, ctx: QualityContext, balance: int, now: datetime,
    only_groups: frozenset[str] | None = None,
) -> Plan:
    """Plan the live balance minus ``keep``. ``only_groups`` limits the plan to those groups (no reserve share)."""
    keep = config.keep
    spendable = max(0, balance - keep)
    genres = config.genres
    counts = {(g.id, ge.id): catalog.batch_counts(g.id, ge.id) for g in GROUPS for ge in genres}
    bought_total = sum(b for b, _ in counts.values())
    # Every unbought batch will spend credits, also manual ones without a genre; rejected tags will not.
    pending_total = sum(n for b, n in catalog.pending_batches().items() if not b.startswith(REJECTED_PREFIX))
    budget = max(0, spendable - pending_total)
    targets = _targets(config, spendable, bought_total)
    quotas = {g.id: genre_quotas(g, genres, targets[g.id]) for g in GROUPS}
    owned_keys = {(r.sample.basename.lower(), r.sample.duration_ms) for r in catalog.licensed_rows()}

    plan_groups = [g for g in GROUPS if only_groups is None or g.id in only_groups]
    cells: dict[tuple[str, str], list[CatalogRow]] = {}
    for group in plan_groups:
        for genre in genres:
            cells[(group.id, genre.id)] = [
                r for r in catalog.rows_for_group(group.id, genre.id) if _eligible(r, group, genre, ctx, owned_keys)
            ]
    remaining = {k: max(0, quotas[k[0]][k[1]] - sum(counts[k])) for k in cells}
    if sum(remaining.values()) > budget:
        # Cells already over quota (bought sounds) would otherwise let the other cells plan past the budget.
        keys = sorted(remaining)
        total = sum(remaining.values())
        remaining = dict(zip(keys, _split(budget, [remaining[k] / total for k in keys])))

    # Scarce cells first, so popular packs do not hit the per-pack total before a thin group is served.
    order = sorted(cells, key=lambda k: (len(cells[k]) / max(1, remaining[k]), k))

    def run(kit_packs: dict[str, set[str]] | None):
        state = _State(config, targets)
        scored_cells: dict[tuple[str, str], list[Scored]] = {}
        picked_cells: dict[tuple[str, str], list[Scored]] = {}
        groups = {g.id: g for g in plan_groups}
        genre_by_id = {g.id: g for g in genres}
        for key in order:
            group, genre = groups[key[0]], genre_by_id[key[1]]
            pool = [
                score_row(r, group.id, genre.id, ctx,
                          kit=bool(kit_packs) and len(kit_packs.get(_pack_key(r.sample), set()) - {group.id}) >= 2)
                for r in cells[key]
            ]
            scored_cells[key] = pool
            picked_cells[key] = _fill_cell(pool, remaining[key], group, genre, state)
        return state, scored_cells, picked_cells

    _, _, pass1 = run(None)
    kit_packs: dict[str, set[str]] = {}
    for (group_id, _genre), picked in pass1.items():
        for c in picked:
            kit_packs.setdefault(_pack_key(c.row.sample), set()).add(group_id)
    state, scored_cells, picked_cells = run(kit_packs)

    items = [
        PlanItem(0, batch_id_for(g, ge, now), g, ge, c.row, SAMPLE_PRICE, c.score, c.why)
        for (g, ge), picked in picked_cells.items() for c in picked
    ]
    capacity = [
        CapacityRow(g, ge, len(cells[(g, ge)]), remaining[(g, ge)], len(picked_cells[(g, ge)]), *counts[(g, ge)])
        for g in (x.id for x in plan_groups) for ge in (x.id for x in genres)
    ]

    if only_groups is None:
        to_place = budget - len(items)
    else:
        to_place = sum(c.quota for c in capacity) - len(items)
    unplaced, reason = 0, ""
    if to_place > 0:
        placed, reason = _final_pass(scored_cells, to_place, state, genres, now, plan_groups)
        items += placed
        unplaced = to_place - len(placed)
        if unplaced == 0:
            reason = ""
    return Plan(order_items(items), balance, keep, spendable, pending_total, capacity, unplaced, reason, now)


def _final_pass(scored_cells, to_place: int, state: _State, genres, now: datetime,
                plan_groups) -> tuple[list[PlanItem], str]:
    """Spend leftover credits on the best remaining sounds in any group; caps still apply."""
    groups = {g.id: g for g in plan_groups}
    genre_by_id = {g.id: g for g in genres}
    best: dict[str, tuple[Scored, str, str]] = {}
    for (group_id, genre_id), pool in scored_cells.items():
        for c in pool:
            h = c.row.sample.file_hash
            if h not in state.chosen and (h not in best or _sort_key(c) < _sort_key(best[h][0])):
                best[h] = (c, group_id, genre_id)
    ordered = sorted(best.values(), key=lambda t: (_sort_key(t[0]), t[1], t[2]))
    placed: list[PlanItem] = []
    for c, group_id, genre_id in ordered:
        if len(placed) >= to_place:
            break
        group, genre = groups[group_id], genre_by_id[genre_id]
        if state.take(c, group, genre):
            placed.append(PlanItem(0, batch_id_for(group_id, genre_id, now), group_id, genre_id, c.row,
                                   SAMPLE_PRICE, c.score, c.why, final_pass=True))
    if len(placed) >= to_place:
        return placed, ""
    return placed, "no candidates left" if not ordered else "caps and duplicates leave no candidates"


def order_items(items: list[PlanItem]) -> list[PlanItem]:
    """Buying order: groups interleaved in proportion to their size, best score first inside a group.

    An early stop then leaves a library that is balanced across groups.
    """
    by_group: dict[str, list[PlanItem]] = {}
    for item in sorted(items, key=lambda i: (-i.score, i.row.sample.file_hash)):
        by_group.setdefault(item.group_id, []).append(item)
    keyed = [
        ((index + 0.5) / len(group_items), -item.score, item.group_id, item.row.sample.file_hash, item)
        for group_items in by_group.values() for index, item in enumerate(group_items)
    ]
    keyed.sort(key=lambda t: t[:4])
    ordered = [t[4] for t in keyed]
    for n, item in enumerate(ordered, 1):
        item.order = n
    return ordered
