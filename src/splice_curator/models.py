"""Plain data types shared by every module."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Sample:
    file_hash: str
    catalog_uuid: str
    name: str
    pack: str | None
    label: str | None
    pack_genre: str | None
    tags: list[tuple[str, str | None]]
    bpm: int | None
    key: str | None
    chord_type: str | None
    duration_ms: int | None
    category: str
    licensed: bool
    preview_url: str | None
    pack_uuid: str | None = None

    @property
    def basename(self) -> str:
        return self.name.rsplit("/", 1)[-1]

    @property
    def tag_labels(self) -> set[str]:
        return {label.lower() for label, _ in self.tags}


@dataclass
class Account:
    username: str
    credits: int | None
    state: str


@dataclass
class AssetFiles:
    name: str
    licensed: bool
    preview_url: str | None
    source_url: str | None = field(repr=False)  # signed URL; keep out of logs
    price: int | None = None  # credits; None when missing or not a whole number of credits


@dataclass
class SearchPage:
    total_records: int
    total_pages: int
    samples: list[Sample]


@dataclass
class Pack:
    uuid: str
    name: str
    genre: str | None
    permalink: str | None
    label: str | None
    published_at: str | None
    tags: list[str]
    sample_count: int  # one-shots plus loops


@dataclass
class PackPage:
    total_records: int
    total_pages: int
    packs: list[Pack]


@dataclass
class ChartPack:
    uuid: str
    name: str
    label: str | None
    position: int
    last_position: int | None
    scope: str  # "global" or a label permalink


@dataclass
class ChartLabel:
    uuid: str
    name: str
    permalink: str | None
    position: int
    last_position: int | None
    pack_count: int | None
    tags: list[str]
