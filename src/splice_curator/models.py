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
