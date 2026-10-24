"""Write and read autobuy plan files, and print the plan summary (spec 5.3 outputs)."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .catalog import Catalog
from .config import Config
from .quality import QualityContext, format_why, percentile
from .review import BATCH_RE, SAMPLE_PAGE, UUID_RE, HASH_RE, write_plan_page
from .selection import Plan
from .sync import AUDIO_EXTENSIONS

CSV_COLUMNS = ["group", "genre", "pack", "label", "score", "why", "link"]
DEFAULT_SOUND_BYTES = 1_500_000
BUYING_SECONDS_PER_SOUND = 19  # measured with download; spec 5.5


@dataclass
class PlanFile:
    plan_id: str
    items: list[dict]  # order, batch_id, group, genre, catalog_uuid, price, score

    @property
    def uuids(self) -> list[str]:
        return [i["catalog_uuid"] for i in self.items]


def plan_id_for(plan: Plan) -> str:
    return f"{plan.created:%Y%m%d-%H%M%S}"


def save_plan(catalog: Catalog, plan: Plan, config: Config, with_page: bool = False) -> dict[str, Path | list[Path]]:
    """Mark the batches in the catalog and write approved files, plan JSON and CSV (and the page)."""
    pid = plan_id_for(plan)
    plans_dir = config.data_dir / "plans"
    approved_dir = config.data_dir / "approved"
    plans_dir.mkdir(parents=True, exist_ok=True)
    approved_dir.mkdir(parents=True, exist_ok=True)

    approved: list[Path] = []
    for batch_id, items in plan.batches.items():
        catalog.mark_shortlisted([i.row.sample.catalog_uuid for i in items], batch_id)
        path = approved_dir / f"approved-{batch_id}.json"
        path.write_text(json.dumps({"batch_id": batch_id, "group": items[0].group_id,
                                    "catalog_uuids": [i.row.sample.catalog_uuid for i in items]}, indent=2))
        approved.append(path)

    body = {
        "plan_id": pid, "created": plan.created.isoformat(timespec="seconds"), "balance": plan.balance,
        "keep": plan.keep, "spendable": plan.spendable,
        "items": [
            {"order": i.order, "batch_id": i.batch_id, "group": i.group_id, "genre": i.genre_id,
             "catalog_uuid": i.row.sample.catalog_uuid, "price": i.price, "score": round(i.score, 4)}
            for i in plan.items
        ],
    }
    json_path = plans_dir / f"plan-{pid}.json"
    json_path.write_text(json.dumps(body, indent=2))

    csv_path = plans_dir / f"plan-{pid}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for i in plan.items:
            s = i.row.sample
            link = SAMPLE_PAGE + s.file_hash if HASH_RE.match(s.file_hash) else ""
            writer.writerow([i.group_id, i.genre_id, s.pack or "", s.label or "", f"{i.score:.3f}",
                             format_why(i.why), link])
    out: dict[str, Path | list[Path]] = {"plan": json_path, "csv": csv_path, "approved": approved}
    if with_page:
        rows = [(i.row.sample, i.group_id, i.genre_id, format_why(i.why)) for i in plan.items]
        out["page"] = write_plan_page(rows, pid, config.data_dir / "review")
    return out


def load_plan(path: Path) -> PlanFile:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read plan {path}: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("plan file: missing items")
    pid = data.get("plan_id")
    if not isinstance(pid, str) or not BATCH_RE.match(pid):
        raise ValueError("plan file: bad plan_id")
    seen: set[str] = set()
    items = []
    for item in data["items"]:
        uuid, batch, price = item.get("catalog_uuid"), item.get("batch_id"), item.get("price")
        if not isinstance(uuid, str) or not UUID_RE.match(uuid) or not isinstance(batch, str) \
                or not BATCH_RE.match(batch) or not isinstance(price, int) or price < 1:
            raise ValueError(f"plan file: bad item {item!r}")
        if uuid in seen:
            continue
        seen.add(uuid)
        items.append(item)
    items.sort(key=lambda i: i.get("order", 0))
    return PlanFile(pid, items)


def estimate_bytes_per_sound(staging: Path) -> int:
    """Average size of downloaded files (first 300 found), or a default."""
    sizes: list[int] = []
    root = Path(staging)
    if root.is_dir():
        for path in root.rglob("*"):
            if path.suffix.lower() in AUDIO_EXTENSIONS and path.is_file():
                sizes.append(path.stat().st_size)
                if len(sizes) >= 300:
                    break
    return int(sum(sizes) / len(sizes)) if sizes else DEFAULT_SOUND_BYTES


def schedule_lines(n: int, config: Config, today: date) -> list[str]:
    """Days needed, latest safe start, and the cap needed if behind."""
    cap = config.max_purchases_per_day
    days_needed = max(1, math.ceil(n / cap)) if n else 0
    days_left = (config.deadline - today).days
    hours = n * BUYING_SECONDS_PER_SOUND / 3600
    lines = [f"buying: {n} credits = {days_needed} buying days at {cap}/day (about {hours:.1f} h in all); "
             f"deadline {config.deadline}, {days_left} days left"]
    start = config.deadline - timedelta(days=days_needed)
    lines.append(f"latest safe start: {start}" + (" (already passed)" if start < today else ""))
    if n and days_left < days_needed:
        need = math.ceil(n / max(1, days_left))
        lines.append(f"BEHIND: needs {need}/day to finish by the deadline; raising max_purchases_per_day is your call")
    return lines


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]


def summarize(
    plan: Plan, ctx: QualityContext, config: Config, today: date, free_bytes: int | None, bytes_per_sound: int,
) -> str:
    """Plain-text summary printed by ``autobuy plan``."""
    lines = [
        f"plan: {plan.total} credits (balance {plan.balance}, keep {plan.keep}, spendable {plan.spendable}, "
        f"already pending {plan.pending})",
    ]
    if plan.unplaced:
        lines.append(f"could not place {plan.unplaced} credits: {plan.unplaced_reason}")
    final = sum(1 for i in plan.items if i.final_pass)
    lines.append(f"placed by the final pass: {final}")

    lines += ["", "capacity (candidates passing filters vs quota still to place)"]
    cap_rows = [["group", "genre", "candidates", "quota", "placed", "shortfall"]]
    cap_rows += [[c.group_id, c.genre_id, str(c.candidates), str(c.quota), str(c.picked), str(c.shortfall)]
                 for c in plan.capacity if c.candidates or c.quota]
    lines += _table(cap_rows)

    lines += ["", "credits by group and genre (including final pass)"]
    counts = Counter((i.group_id, i.genre_id) for i in plan.items)
    by_group = Counter(i.group_id for i in plan.items)
    rows = [["group", "genre", "credits"]] + [[g, ge, str(n)] for (g, ge), n in sorted(counts.items())]
    lines += _table(rows)
    lines.append("by group: " + ", ".join(f"{g} {n}" for g, n in sorted(by_group.items())))

    if plan.items:
        packs = [ctx.pack_uuid(i.row.sample) for i in plan.items]
        charted = sum(1 for p in packs if p and ctx.chart_packs.get(p))
        cutoff = ctx.today - timedelta(days=30.44 * config.recent_months)
        recent = sum(1 for p in packs if p and ctx.pack_published.get(p, datetime.min) >= cutoff)
        n = len(plan.items)
        lines += ["", f"from charted packs: {100 * charted / n:.0f}%   from recent packs "
                      f"(last {config.recent_months} months): {100 * recent / n:.0f}%"]
        top_packs = Counter(i.row.sample.pack or "?" for i in plan.items).most_common(8)
        top_labels = Counter(i.row.sample.label or "?" for i in plan.items).most_common(8)
        lines.append("top packs: " + "; ".join(f"{k} ({v})" for k, v in top_packs))
        lines.append("top labels: " + "; ".join(f"{k} ({v})" for k, v in top_labels))
        scores = [i.score for i in plan.items]
        lines.append("score percentiles (10/25/50/75/90): " + " / ".join(
            f"{percentile(scores, p):.2f}" for p in (10, 25, 50, 75, 90)))

    need = plan.total * bytes_per_sound
    store = f"storage: about {need / 1e9:.1f} GB needed (at {bytes_per_sound / 1e6:.1f} MB per sound)"
    if free_bytes is not None:
        store += f", {free_bytes / 1e9:.1f} GB free" + ("  -- NOT ENOUGH SPACE" if need > free_bytes else "")
    lines += ["", store]
    lines += schedule_lines(plan.total, config, today)
    return "\n".join(lines)
