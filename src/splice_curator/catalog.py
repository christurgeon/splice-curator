"""Local SQLite catalog of search results, ownership, and purchases."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .models import Sample

PURCHASE_RESULTS = ("bought", "skipped_owned", "uncertain", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    file_hash TEXT PRIMARY KEY,
    catalog_uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    pack TEXT,
    label TEXT,
    pack_genre TEXT,
    tags TEXT NOT NULL,
    bpm INTEGER,
    key TEXT,
    chord_type TEXT,
    duration_ms INTEGER,
    category TEXT NOT NULL,
    licensed INTEGER NOT NULL DEFAULT 0,
    preview_url TEXT,
    best_position INTEGER NOT NULL,
    best_query TEXT,
    local_path TEXT,
    shortlisted_batch TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sample_groups (
    file_hash TEXT NOT NULL REFERENCES samples(file_hash),
    grp TEXT NOT NULL,
    PRIMARY KEY (file_hash, grp)
);
CREATE TABLE IF NOT EXISTS searches (
    query TEXT,
    grp TEXT,
    page INTEGER NOT NULL,
    run_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    catalog_uuid TEXT NOT NULL,
    result TEXT NOT NULL,
    credits_before INTEGER,
    credits_after INTEGER
);
CREATE INDEX IF NOT EXISTS purchases_day ON purchases(day, result);
"""


@dataclass
class CatalogRow:
    sample: Sample
    best_position: int
    groups: set[str]
    local_path: str | None
    shortlisted_batch: str | None


class Catalog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- writes ---------------------------------------------------------------

    def record_search(
        self, samples: list[Sample], query: str, group: str | None, page: int, page_size: int
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                "INSERT INTO searches(query, grp, page, run_at) VALUES (?, ?, ?, ?)",
                (query, group, page, now),
            )
            for index, s in enumerate(samples):
                position = (page - 1) * page_size + index
                self.db.execute(
                    """
                    INSERT INTO samples (file_hash, catalog_uuid, name, pack, label, pack_genre, tags,
                        bpm, key, chord_type, duration_ms, category, licensed, preview_url,
                        best_position, best_query, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_hash) DO UPDATE SET
                        catalog_uuid = excluded.catalog_uuid,
                        name = excluded.name,
                        pack = excluded.pack,
                        label = excluded.label,
                        pack_genre = excluded.pack_genre,
                        tags = excluded.tags,
                        bpm = excluded.bpm,
                        key = excluded.key,
                        chord_type = excluded.chord_type,
                        duration_ms = excluded.duration_ms,
                        category = excluded.category,
                        licensed = MAX(samples.licensed, excluded.licensed),
                        preview_url = excluded.preview_url,
                        best_query = CASE WHEN excluded.best_position < samples.best_position
                                     THEN excluded.best_query ELSE samples.best_query END,
                        best_position = MIN(samples.best_position, excluded.best_position),
                        updated_at = excluded.updated_at
                    """,
                    (
                        s.file_hash, s.catalog_uuid, s.name, s.pack, s.label, s.pack_genre,
                        json.dumps(s.tags), s.bpm, s.key, s.chord_type, s.duration_ms,
                        s.category, int(s.licensed), s.preview_url, position, query, now,
                    ),
                )
                if group:
                    self.db.execute(
                        "INSERT OR IGNORE INTO sample_groups(file_hash, grp) VALUES (?, ?)",
                        (s.file_hash, group),
                    )

    def mark_licensed(self, catalog_uuid: str) -> None:
        with self.db:
            self.db.execute("UPDATE samples SET licensed = 1 WHERE catalog_uuid = ?", (catalog_uuid,))

    def mark_shortlisted(self, catalog_uuids: list[str], batch_id: str) -> None:
        with self.db:
            self.db.executemany(
                "UPDATE samples SET shortlisted_batch = ? WHERE catalog_uuid = ?",
                [(batch_id, u) for u in catalog_uuids],
            )

    def set_local_path(self, file_hash: str, path: Path) -> None:
        with self.db:
            self.db.execute("UPDATE samples SET local_path = ? WHERE file_hash = ?", (str(path), file_hash))

    def log_purchase(
        self,
        catalog_uuid: str,
        result: str,
        credits_before: int | None,
        credits_after: int | None,
        at: datetime,
    ) -> None:
        if result not in PURCHASE_RESULTS:
            raise ValueError(f"unknown purchase result {result!r}")
        with self.db:
            self.db.execute(
                "INSERT INTO purchases(at, day, catalog_uuid, result, credits_before, credits_after)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (at.isoformat(timespec="seconds"), at.date().isoformat(), catalog_uuid, result,
                 credits_before, credits_after),
            )

    # -- reads ----------------------------------------------------------------

    def purchases_on(self, day: date) -> int:
        (count,) = self.db.execute(
            "SELECT COUNT(*) FROM purchases WHERE day = ? AND result = 'bought'", (day.isoformat(),)
        ).fetchone()
        return count

    def _rows(self, where: str, params: tuple) -> list[CatalogRow]:
        rows = self.db.execute(
            f"SELECT * FROM samples {where} ORDER BY best_position, file_hash", params
        ).fetchall()
        groups: dict[str, set[str]] = {}
        if rows:
            hashes = [r["file_hash"] for r in rows]
            for i in range(0, len(hashes), 500):
                chunk = hashes[i : i + 500]
                marks = ",".join("?" * len(chunk))
                for g in self.db.execute(
                    f"SELECT file_hash, grp FROM sample_groups WHERE file_hash IN ({marks})", chunk
                ):
                    groups.setdefault(g["file_hash"], set()).add(g["grp"])
        return [self._to_row(r, groups.get(r["file_hash"], set())) for r in rows]

    @staticmethod
    def _to_row(r: sqlite3.Row, groups: set[str]) -> CatalogRow:
        sample = Sample(
            file_hash=r["file_hash"],
            catalog_uuid=r["catalog_uuid"],
            name=r["name"],
            pack=r["pack"],
            label=r["label"],
            pack_genre=r["pack_genre"],
            tags=[(label, tax) for label, tax in json.loads(r["tags"])],
            bpm=r["bpm"],
            key=r["key"],
            chord_type=r["chord_type"],
            duration_ms=r["duration_ms"],
            category=r["category"],
            licensed=bool(r["licensed"]),
            preview_url=r["preview_url"],
        )
        return CatalogRow(sample, r["best_position"], groups, r["local_path"], r["shortlisted_batch"])

    def rows_for_group(self, group: str) -> list[CatalogRow]:
        return self._rows(
            "WHERE file_hash IN (SELECT file_hash FROM sample_groups WHERE grp = ?)", (group,)
        )

    def get_by_catalog_uuid(self, catalog_uuid: str) -> CatalogRow | None:
        rows = self._rows("WHERE catalog_uuid = ?", (catalog_uuid,))
        return rows[0] if rows else None

    def licensed_rows(self) -> list[CatalogRow]:
        return self._rows("WHERE licensed = 1", ())

    def all_rows(self) -> list[CatalogRow]:
        return self._rows("", ())
