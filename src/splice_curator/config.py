"""Settings: defaults, then the TOML config file, then per-run overrides."""

from __future__ import annotations

import dataclasses
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .budget import GENRES, Genre

DEFAULT_CONFIG_PATH = Path("~/.splice-curator/config.toml").expanduser()
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
DELAY_KINDS = ("search", "account", "asset", "purchase", "download")
_PATH_FIELDS = ("data_dir", "staging_dir", "splice_dir")


@dataclass(frozen=True)
class Delay:
    min_s: float
    max_s: float

    def __post_init__(self) -> None:
        if self.min_s < 0 or self.max_s < self.min_s:
            raise ValueError(f"bad delay range {self.min_s}..{self.max_s}")


def default_delays() -> dict[str, Delay]:
    return {
        "search": Delay(3, 6),
        "account": Delay(3, 6),
        "asset": Delay(3, 6),
        "purchase": Delay(6, 12),
        "download": Delay(2, 5),
    }


@dataclass
class Config:
    data_dir: Path
    staging_dir: Path
    splice_dir: Path
    delays: dict[str, Delay] = field(default_factory=default_delays)
    max_purchases_per_day: int = 400
    max_purchases_per_run: int | None = None
    pause_every: int = 50
    pause_seconds: float = 60.0
    max_tries: int = 4
    backoff_base_s: float = 5.0
    min_free_bytes: int = 2 * 1024**3
    per_pack_cap: int = 5
    batch_size: int = 200
    user_agent: str = DEFAULT_USER_AGENT
    anonymous_id: str = ""
    refresh_margin_s: int = 300
    refresh_poll_s: float = 30.0
    refresh_wait_s: float = 900.0
    genres: tuple[Genre, ...] = GENRES  # enabled genres only

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "catalog.sqlite"


def _defaults() -> dict:
    home = Path.home()
    return {
        "data_dir": home / ".splice-curator",
        "staging_dir": home / "SpliceCurator" / "downloads",
        "splice_dir": home / "Splice",
        "delays": default_delays(),
        "genres": {g.id: g for g in GENRES},
    }


def _merge(values: dict, extra: dict) -> None:
    known = {f.name for f in dataclasses.fields(Config)}
    for key, value in extra.items():
        if key not in known:
            raise ValueError(f"unknown config key: {key}")
        if key == "delays":
            for kind, delay in value.items():
                if kind not in DELAY_KINDS:
                    raise ValueError(f"unknown delay kind: {kind}")
                if not isinstance(delay, Delay):
                    delay = Delay(float(delay["min_s"]), float(delay["max_s"]))
                values["delays"][kind] = delay
        elif key == "genres":
            for genre_id, table in value.items():
                values["genres"][genre_id] = _genre_from_table(genre_id, table, values["genres"].get(genre_id))
        elif key in _PATH_FIELDS:
            values[key] = Path(value).expanduser()
        else:
            values[key] = value


def _genre_from_table(genre_id: str, table: dict, base: Genre | None) -> Genre:
    """Apply a ``[genres.<id>]`` table over a known genre; a new id needs every key."""
    keys = {"title", "weight", "tags", "prefixes", "bpm"}
    if unknown := set(table) - keys:
        raise ValueError(f"genres.{genre_id}: unknown keys {sorted(unknown)}")
    if base is None and (missing := keys - set(table)):
        raise ValueError(f"genres.{genre_id}: new genre needs keys {sorted(missing)}")
    bpm = table.get("bpm", base.bpm if base else None)
    if len(bpm) != 2 or bpm[0] > bpm[1]:
        raise ValueError(f"genres.{genre_id}: bpm must be [low, high]")
    weight = float(table.get("weight", base.weight if base else 0))
    if weight < 0:
        raise ValueError(f"genres.{genre_id}: weight must not be negative")
    return Genre(
        genre_id,
        table.get("title", base.title if base else ""),
        weight,
        frozenset(t.lower() for t in table["tags"]) if "tags" in table else base.tags,
        tuple(table["prefixes"]) if "prefixes" in table else base.prefixes,
        (int(bpm[0]), int(bpm[1])),
    )


def _enabled_genres(genres: dict[str, Genre]) -> tuple[Genre, ...]:
    enabled = tuple(g for g in genres.values() if g.weight > 0)
    total = sum(g.weight for g in enabled)
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"genre weights must sum to 1.0; enabled weights sum to {total:g}")
    return enabled


def _persist_anonymous_id(path: Path, anonymous_id: str) -> None:
    # Prepend as a top-level key so it stays outside any [table].
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(f'anonymous_id = "{anonymous_id}"\n' + old, encoding="utf-8")


def load_config(path: Path | None = None, overrides: dict | None = None) -> Config:
    """Build a Config. ``path=None`` means no file (pure defaults)."""
    values = _defaults()
    if path is not None:
        path = Path(path).expanduser()
        if path.exists():
            _merge(values, tomllib.loads(path.read_text(encoding="utf-8")))
        if not values.get("anonymous_id"):
            values["anonymous_id"] = str(uuid.uuid4())
            _persist_anonymous_id(path, values["anonymous_id"])
    else:
        values["anonymous_id"] = str(uuid.uuid4())
    if overrides:
        _merge(values, overrides)
    values["genres"] = _enabled_genres(values["genres"])
    return Config(**values)
