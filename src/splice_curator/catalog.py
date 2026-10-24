"""Local SQLite catalog of search results, ownership, and purchases."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .models import ChartLabel, ChartPack, Pack, Sample

UNRANKED = 10**9  # position for rows not found by a ranked search
PURCHASE_RESULTS = (
    "bought", "skipped_owned", "uncertain", "failed",
    "price_unknown", "price_mismatch",
    "not_bought",  # an uncertain purchase that Splice shows as not licensed
)

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
CREATE TABLE IF NOT EXISTS sample_genres (
    file_hash TEXT NOT NULL REFERENCES samples(file_hash),
    grp TEXT NOT NULL,
    genre TEXT NOT NULL,
    subtype TEXT NOT NULL,
    PRIMARY KEY (file_hash, grp, genre, subtype)
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

SCHEMA_VERSION = 1
SAMPLE_COLUMNS_V1 = (
    ("pack_uuid", "TEXT"),
    ("in_pack_position", "INTEGER"),
    ("in_pack_count", "INTEGER"),
    ("price", "INTEGER"),
)
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS packs (
    uuid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    main_genre TEXT,
    permalink TEXT,
    label TEXT,
    published_at TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    sample_count INTEGER NOT NULL DEFAULT 0,
    samples_fetched_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pack_ranks (
    pack_uuid TEXT NOT NULL REFERENCES packs(uuid),
    genre TEXT NOT NULL,
    all_time_rank INTEGER NOT NULL,
    recent_rank INTEGER,
    PRIMARY KEY (pack_uuid, genre)
);
CREATE TABLE IF NOT EXISTS chart_packs (
    scope TEXT NOT NULL,
    pack_uuid TEXT NOT NULL,
    name TEXT NOT NULL,
    label TEXT,
    position INTEGER NOT NULL,
    last_position INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (scope, pack_uuid)
);
CREATE TABLE IF NOT EXISTS chart_labels (
    uuid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    permalink TEXT,
    position INTEGER NOT NULL,
    last_position INTEGER,
    pack_count INTEGER,
    tags TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL
);
"""


@dataclass
class CatalogRow:
    sample: Sample
    best_position: int
    groups: set[str]
    local_path: str | None
    shortlisted_batch: str | None
    genres: set[tuple[str, str]] = field(default_factory=set)  # (genre, subtype) for the loaded group
    in_pack_position: int | None = None  # 0-based popularity position inside the sample's pack
    in_pack_count: int | None = None  # sounds of this category in the pack (for the percentile)
    price: int | None = None


class Catalog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an older database up to ``SCHEMA_VERSION`` (tracked in ``PRAGMA user_version``)."""
        (version,) = self.db.execute("PRAGMA user_version").fetchone()
        if version >= SCHEMA_VERSION:
            return
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(samples)")}
        with self.db:
            for name, kind in SAMPLE_COLUMNS_V1:
                if name not in have:
                    self.db.execute(f"ALTER TABLE samples ADD COLUMN {name} {kind}")
            for statement in SCHEMA_V1.split(";"):
                if statement.strip():
                    self.db.execute(statement)
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.db.close()

    # -- writes ---------------------------------------------------------------

    def record_search(
        self,
        samples: list[Sample],
        query: str,
        group: str | None,
        page: int,
        page_size: int,
        ranked: bool = True,
        genre: str | None = None,
        subtype: str | None = None,
    ) -> None:
        """Upsert results. ``ranked=False`` (the owned-library listing) leaves positions alone."""
        now = datetime.now().isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                "INSERT INTO searches(query, grp, page, run_at) VALUES (?, ?, ?, ?)",
                (query, group, page, now),
            )
            for index, s in enumerate(samples):
                position = (page - 1) * page_size + index
                if not ranked:
                    position += UNRANKED
                self.db.execute(
                    """
                    INSERT INTO samples (file_hash, catalog_uuid, name, pack, label, pack_genre, tags,
                        bpm, key, chord_type, duration_ms, category, licensed, preview_url,
                        best_position, best_query, updated_at, pack_uuid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        updated_at = excluded.updated_at,
                        pack_uuid = COALESCE(excluded.pack_uuid, samples.pack_uuid)
                    """,
                    (
                        s.file_hash, s.catalog_uuid, s.name, s.pack, s.label, s.pack_genre,
                        json.dumps(s.tags), s.bpm, s.key, s.chord_type, s.duration_ms,
                        s.category, int(s.licensed), s.preview_url, position, query, now, s.pack_uuid,
                    ),
                )
                if group:
                    self.db.execute(
                        "INSERT OR IGNORE INTO sample_groups(file_hash, grp) VALUES (?, ?)",
                        (s.file_hash, group),
                    )
                    if genre and subtype:
                        self.db.execute(
                            "INSERT OR IGNORE INTO sample_genres(file_hash, grp, genre, subtype)"
                            " VALUES (?, ?, ?, ?)",
                            (s.file_hash, group, genre, subtype),
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

    def record_pack_samples(self, samples: list[Sample], pack_uuid: str, first_position: int, count: int) -> None:
        """Upsert sounds found inside a pack; positions go to the in-pack columns only.

        New rows get the UNRANKED query position, so in-pack and query positions never mix.
        """
        now = datetime.now().isoformat(timespec="seconds")
        with self.db:
            for index, s in enumerate(samples):
                self.db.execute(
                    """
                    INSERT INTO samples (file_hash, catalog_uuid, name, pack, label, pack_genre, tags,
                        bpm, key, chord_type, duration_ms, category, licensed, preview_url,
                        best_position, best_query, updated_at, pack_uuid, in_pack_position, in_pack_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                    ON CONFLICT(file_hash) DO UPDATE SET
                        catalog_uuid = excluded.catalog_uuid, name = excluded.name, pack = excluded.pack,
                        label = excluded.label, pack_genre = excluded.pack_genre, tags = excluded.tags,
                        bpm = excluded.bpm, key = excluded.key, chord_type = excluded.chord_type,
                        duration_ms = excluded.duration_ms, category = excluded.category,
                        licensed = MAX(samples.licensed, excluded.licensed),
                        preview_url = excluded.preview_url, updated_at = excluded.updated_at,
                        pack_uuid = excluded.pack_uuid,
                        in_pack_position = MIN(COALESCE(samples.in_pack_position, excluded.in_pack_position),
                                               excluded.in_pack_position),
                        in_pack_count = excluded.in_pack_count
                    """,
                    (
                        s.file_hash, s.catalog_uuid, s.name, s.pack, s.label, s.pack_genre,
                        json.dumps(s.tags), s.bpm, s.key, s.chord_type, s.duration_ms, s.category,
                        int(s.licensed), s.preview_url, UNRANKED, now, pack_uuid, first_position + index, count,
                    ),
                )

    def assign(self, file_hash: str, group: str, genre: str, subtype: str) -> None:
        """Make a sound a candidate for ``group`` x ``genre`` (so ``rows_for_group`` finds it)."""
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO sample_groups(file_hash, grp) VALUES (?, ?)", (file_hash, group))
            self.db.execute(
                "INSERT OR IGNORE INTO sample_genres(file_hash, grp, genre, subtype) VALUES (?, ?, ?, ?)",
                (file_hash, group, genre, subtype),
            )

    def set_price(self, catalog_uuid: str, price: int) -> None:
        with self.db:
            self.db.execute("UPDATE samples SET price = ? WHERE catalog_uuid = ?", (price, catalog_uuid))

    def upsert_packs(self, packs: list[Pack]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.db:
            for p in packs:
                self.db.execute(
                    """
                    INSERT INTO packs(uuid, name, main_genre, permalink, label, published_at, tags,
                                      sample_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(uuid) DO UPDATE SET name = excluded.name, main_genre = excluded.main_genre,
                        permalink = excluded.permalink, label = excluded.label,
                        published_at = excluded.published_at, tags = excluded.tags,
                        sample_count = excluded.sample_count, updated_at = excluded.updated_at
                    """,
                    (p.uuid, p.name, p.genre, p.permalink, p.label, p.published_at,
                     json.dumps(p.tags), p.sample_count, now),
                )

    def set_pack_rank(self, pack_uuid: str, genre: str, all_time_rank: int, recent_rank: int | None) -> None:
        with self.db:
            self.db.execute(
                """
                INSERT INTO pack_ranks(pack_uuid, genre, all_time_rank, recent_rank) VALUES (?, ?, ?, ?)
                ON CONFLICT(pack_uuid, genre) DO UPDATE SET all_time_rank = excluded.all_time_rank,
                    recent_rank = excluded.recent_rank
                """,
                (pack_uuid, genre, all_time_rank, recent_rank),
            )

    def clear_pack_ranks(self, genre: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM pack_ranks WHERE genre = ?", (genre,))

    def mark_pack_fetched(self, pack_uuid: str, at: datetime) -> None:
        with self.db:
            self.db.execute("UPDATE packs SET samples_fetched_at = ? WHERE uuid = ?",
                            (at.isoformat(timespec="seconds"), pack_uuid))

    def replace_chart_packs(self, scope: str, charted: list[ChartPack], at: datetime) -> None:
        stamp = at.isoformat(timespec="seconds")
        with self.db:
            self.db.execute("DELETE FROM chart_packs WHERE scope = ?", (scope,))
            self.db.executemany(
                "INSERT OR REPLACE INTO chart_packs(scope, pack_uuid, name, label, position, last_position, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(scope, c.uuid, c.name, c.label, c.position, c.last_position, stamp) for c in charted],
            )

    def replace_chart_labels(self, charted: list[ChartLabel], at: datetime) -> None:
        stamp = at.isoformat(timespec="seconds")
        with self.db:
            self.db.execute("DELETE FROM chart_labels")
            self.db.executemany(
                "INSERT OR REPLACE INTO chart_labels(uuid, name, permalink, position, last_position, pack_count,"
                " tags, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(c.uuid, c.name, c.permalink, c.position, c.last_position, c.pack_count,
                  json.dumps(c.tags), stamp) for c in charted],
            )

    # -- pack and chart reads -------------------------------------------------

    def chart_fetched_at(self, scope: str | None) -> datetime | None:
        """When chart ``scope`` ('global' or a label permalink; None means the labels chart) was stored."""
        if scope is None:
            row = self.db.execute("SELECT MAX(fetched_at) AS t FROM chart_labels").fetchone()
        else:
            row = self.db.execute("SELECT MAX(fetched_at) AS t FROM chart_packs WHERE scope = ?", (scope,)).fetchone()
        return datetime.fromisoformat(row["t"]) if row["t"] else None

    def chart_pack_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM chart_packs ORDER BY scope, position").fetchall()

    def chart_label_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM chart_labels ORDER BY position").fetchall()

    def pack_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM packs ORDER BY name, uuid").fetchall()

    def pack_rank_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM pack_ranks ORDER BY genre, all_time_rank").fetchall()

    def pack_fetched_at(self, pack_uuid: str) -> datetime | None:
        row = self.db.execute("SELECT samples_fetched_at AS t FROM packs WHERE uuid = ?", (pack_uuid,)).fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    # -- reads ----------------------------------------------------------------

    def purchases_on(self, day: date) -> int:
        (count,) = self.db.execute(
            "SELECT COUNT(*) FROM purchases WHERE day = ? AND result = 'bought'", (day.isoformat(),)
        ).fetchone()
        return count

    def unresolved_uncertain(self) -> list[str]:
        """Sounds with an `uncertain` purchase log row and no later outcome, not known as owned."""
        rows = self.db.execute(
            """
            SELECT DISTINCT p.catalog_uuid FROM purchases p
            JOIN samples s ON s.catalog_uuid = p.catalog_uuid
            WHERE p.result = 'uncertain' AND s.licensed = 0
              AND NOT EXISTS (SELECT 1 FROM purchases q WHERE q.catalog_uuid = p.catalog_uuid
                              AND q.id > p.id AND q.result IN ('bought', 'skipped_owned', 'not_bought'))
            ORDER BY p.id
            """
        ).fetchall()
        return [r["catalog_uuid"] for r in rows]

    def _rows(self, where: str, params: tuple, genre_group: str | None = None) -> list[CatalogRow]:
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
        genres: dict[str, set[tuple[str, str]]] = {}
        if rows and genre_group:
            for i in range(0, len(hashes), 500):
                chunk = hashes[i : i + 500]
                marks = ",".join("?" * len(chunk))
                for g in self.db.execute(
                    "SELECT file_hash, genre, subtype FROM sample_genres"
                    f" WHERE grp = ? AND file_hash IN ({marks})", [genre_group, *chunk]
                ):
                    genres.setdefault(g["file_hash"], set()).add((g["genre"], g["subtype"]))
        return [
            self._to_row(r, groups.get(r["file_hash"], set()), genres.get(r["file_hash"], set()))
            for r in rows
        ]

    @staticmethod
    def _to_row(r: sqlite3.Row, groups: set[str], genres: set[tuple[str, str]]) -> CatalogRow:
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
            pack_uuid=r["pack_uuid"],
        )
        return CatalogRow(
            sample, r["best_position"], groups, r["local_path"], r["shortlisted_batch"], genres,
            r["in_pack_position"], r["in_pack_count"], r["price"],
        )

    def rows_for_group(self, group: str, genre: str | None = None) -> list[CatalogRow]:
        if genre:
            return self._rows(
                "WHERE file_hash IN (SELECT file_hash FROM sample_genres WHERE grp = ? AND genre = ?)",
                (group, genre), group,
            )
        return self._rows(
            "WHERE file_hash IN (SELECT file_hash FROM sample_groups WHERE grp = ?)", (group,), group
        )

    def count_spent(self, group: str, genre: str) -> int:
        """Rows shortlisted in a batch for this group and genre (bought or not).

        Genres overlap, so a sound counts only against the genre of the batch that
        picked it, never against every genre whose searches found it.
        """
        prefix = f"{group}-{genre}-"
        (count,) = self.db.execute(
            "SELECT COUNT(*) FROM samples WHERE substr(shortlisted_batch, 1, ?) = ?",
            (len(prefix), prefix),
        ).fetchone()
        return count

    def rows_for_pack(self, pack_uuid: str) -> list[CatalogRow]:
        return self._rows("WHERE pack_uuid = ?", (pack_uuid,))

    def get_by_catalog_uuid(self, catalog_uuid: str) -> CatalogRow | None:
        rows = self._rows("WHERE catalog_uuid = ?", (catalog_uuid,))
        return rows[0] if rows else None

    def licensed_rows(self) -> list[CatalogRow]:
        return self._rows("WHERE licensed = 1", ())

    def all_rows(self) -> list[CatalogRow]:
        return self._rows("", ())
