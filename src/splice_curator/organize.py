"""Build the organized, exFAT-safe library from local files (spec 7.8).

Files are copied, never moved or linked, and sources are only read.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .budget import TARGET_GENRES
from .catalog import Catalog, CatalogRow
from .models import Sample
from .paths import safe_part
from .sync import AUDIO_EXTENSIONS

ONESHOT_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("Kicks", frozenset({"kicks"})),
    ("Snares & Claps", frozenset({"snares", "claps"})),
    ("Hats & Cymbals", frozenset({"hats", "cymbals", "rides", "crashes"})),
    ("Percussion", frozenset({"percussion", "shakers", "congas", "bongos", "toms", "rims"})),
    ("Bass", frozenset({"bass", "808"})),
    ("Vocals", frozenset({"vocals"})),
    ("FX", frozenset({"fx"})),
    ("Synth", frozenset({"synth", "chords", "stabs", "keys", "piano"})),
)
CSV_COLUMNS = ["path", "pack", "label", "genres", "tags", "bpm", "key", "category"]


@dataclass
class OrganizeReport:
    copied: int
    skipped: int
    unsorted: int


def format_key(key: str | None, chord_type: str | None) -> str | None:
    if not key:
        return None
    name = key[0].upper() + key[1:]
    return name + ("m" if (chord_type or "").lower() == "minor" else "")


def target_name(sample: Sample) -> str:
    parts = [str(sample.bpm) if sample.bpm else None, format_key(sample.key, sample.chord_type), sample.basename]
    return safe_part("_".join(p for p in parts if p))


def classify(sample: Sample) -> tuple[str, str]:
    tags = sample.tag_labels
    if sample.category == "loop":
        if "vocals" in tags:
            return "Loops", "Vocals"
        if tags & {"bass", "808"}:
            return "Loops", "Bass"
        if tags & {"chords", "keys", "piano", "synth", "stabs"}:
            return "Loops", "Chords & Keys"
        if "percussion" in tags and not tags & {"kicks", "snares", "claps"}:
            return "Loops", "Percussion"
        if tags & {"drums", "kicks", "snares", "claps", "hats", "tops", "percussion"}:
            return "Loops", "Drums"
        return "Loops", "Other"
    for sub, wanted in ONESHOT_RULES:
        if tags & wanted:
            return "One-Shots", sub
    return "One-Shots", "Other"


def _source_files(root: Path):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.suffix.lower() in AUDIO_EXTENSIONS and path.is_file():
            yield path


def _match(path: Path, by_name: dict[str, list[CatalogRow]], by_local: dict[str, CatalogRow]) -> CatalogRow | None:
    if str(path) in by_local:
        return by_local[str(path)]
    candidates = by_name.get(path.name.lower(), [])
    if len(candidates) > 1:
        folders = {p.lower() for p in path.parts[:-1]}
        for row in candidates:
            pack = row.sample.pack or row.sample.name.split("/", 1)[0]
            if pack.lower() in folders or safe_part(pack).lower() in folders:
                return row
    return candidates[0] if candidates else None


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name("." + dest.name + ".partial")
    try:
        shutil.copyfile(src, partial)
        try:
            shutil.copystat(src, partial)
        except OSError:
            pass  # exFAT has no POSIX permissions or xattrs
        os.replace(partial, dest)
    finally:
        partial.unlink(missing_ok=True)


def _place(src: Path, dest: Path, pack: str | None) -> tuple[Path, bool]:
    """Pick a destination that never overwrites a different file.

    Returns ``(dest, needs_copy)``.
    """
    size = src.stat().st_size
    stem, ext = os.path.splitext(dest.name)
    options = [dest]
    if pack:
        options.append(dest.with_name(safe_part(f"{stem}__{pack}{ext}")))
    options += [dest.with_name(safe_part(f"{stem}__{i}{ext}")) for i in range(2, 100)]
    for option in options:
        if not option.exists():
            return option, True
        if option.is_file() and option.stat().st_size == size:
            return option, False
    raise OSError(f"too many files named like {dest.name}")


def _csv_row(rel: str, sample: Sample | None) -> dict:
    if sample is None:
        return {"path": rel}
    genres = {label for label, tax in sample.tags if (tax or "").lower() == "genre" or label.lower() in TARGET_GENRES}
    if sample.pack_genre:
        genres.add(sample.pack_genre)
    return {
        "path": rel,
        "pack": sample.pack or "",
        "label": sample.label or "",
        "genres": "; ".join(sorted(genres, key=str.lower)),
        "tags": "; ".join(label for label, _ in sample.tags),
        "bpm": sample.bpm if sample.bpm is not None else "",
        "key": format_key(sample.key, sample.chord_type) or "",
        "category": sample.category,
    }


def _check_target(sources: list[Path], target: Path) -> None:
    t = target.resolve()
    for src in sources:
        s = src.resolve()
        if t == s or s in t.parents or t in s.parents:
            raise ValueError(f"target {target} must not overlap source {src}")


def run_organize(catalog: Catalog, sources: list[Path], target: Path) -> OrganizeReport:
    sources = [Path(s) for s in sources]
    target = Path(target)
    _check_target(sources, target)
    root = target / "Samples"
    root.mkdir(parents=True, exist_ok=True)

    by_name: dict[str, list[CatalogRow]] = {}
    by_local: dict[str, CatalogRow] = {}
    for row in catalog.all_rows():
        by_name.setdefault(row.sample.basename.lower(), []).append(row)
        if row.local_path:
            by_local[row.local_path] = row

    report = OrganizeReport(copied=0, skipped=0, unsorted=0)
    entries: dict[str, dict] = {}
    for source in sources:
        for path in _source_files(source):
            row = _match(path, by_name, by_local)
            if row is None:
                report.unsorted += 1
                dest = root / "Unsorted" / safe_part(path.name)
                sample, pack = None, None
            else:
                sample = row.sample
                top, sub = classify(sample)
                dest = root / top / sub / target_name(sample)
                pack = sample.pack
            dest, needs_copy = _place(path, dest, pack)
            if needs_copy:
                _copy(path, dest)
                report.copied += 1
            else:
                report.skipped += 1
            rel = dest.relative_to(root).as_posix()
            entries[rel] = _csv_row(rel, sample)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for rel in sorted(entries):
        writer.writerow(entries[rel])
    csv_path = root / "catalog.csv"
    tmp = root / ".catalog.csv.partial"
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    os.replace(tmp, csv_path)
    return report
