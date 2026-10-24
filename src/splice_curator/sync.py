"""Download every owned sound that is not on disk yet."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .catalog import Catalog, CatalogRow
from .client import AuthRejected, RateLimited, SpliceClient, SpliceError
from .paths import safe_part

AUDIO_EXTENSIONS = {".wav", ".aif", ".aiff", ".flac", ".mp3"}
PAGE_SIZE = 50


@dataclass
class SyncReport:
    downloaded: int
    skipped: int
    stopped_reason: str | None
    failed: list[str] = field(default_factory=list)  # catalog_uuids that could not be downloaded


def index_local(dirs: list[Path]) -> dict[str, Path]:
    """Lowercase basename -> path for audio files. Earlier dirs win."""
    index: dict[str, Path] = {}
    for root in dirs:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if path.suffix.lower() in AUDIO_EXTENSIONS and path.is_file():
                index.setdefault(path.name.lower(), path)
    return index


def refresh_owned(client: SpliceClient, catalog: Catalog) -> int:
    """Record every licensed sample in the catalog; return how many."""
    count = 0
    page = 1
    while True:
        result = client.search(None, None, page, limit=PAGE_SIZE, licensed=True)
        for sample in result.samples:
            sample.licensed = True
        catalog.record_search(result.samples, "", None, page, PAGE_SIZE, ranked=False)
        count += len(result.samples)
        if not result.samples or page >= result.total_pages:
            return count
        page += 1


def staging_path(staging: Path, name: str) -> Path:
    """Splice's pack path under ``staging``, each part made safe."""
    parts = [safe_part(p) for p in name.split("/") if p not in ("", ".", "..")]
    return Path(staging).joinpath(*(parts or ["_"]))  # never ``staging`` itself (its .partial would sit outside)


def download_one(client: SpliceClient, catalog: Catalog, row: CatalogRow, staging: Path) -> Path:
    sample = row.sample
    files = client.asset_files(sample.catalog_uuid)
    if not files.licensed or not files.source_url:
        raise SpliceError(f"{sample.catalog_uuid} is not licensed or has no source file")
    dest = staging_path(staging, sample.name)
    client.download(files.source_url, dest)
    catalog.set_local_path(sample.file_hash, dest)
    return dest


def run_sync(
    client: SpliceClient,
    catalog: Catalog,
    staging: Path,
    local_dirs: list[Path],
    min_free_bytes: int,
    limit: int | None = None,
    disk_usage: Callable = shutil.disk_usage,
) -> SyncReport:
    staging = Path(staging)
    report = SyncReport(downloaded=0, skipped=0, stopped_reason=None)
    if not staging.parent.is_dir():
        # Likely an unplugged drive: never create the path on the wrong disk.
        report.stopped_reason = f"staging folder's parent is missing: {staging.parent} (is the drive connected?)"
        return report
    local = index_local(local_dirs)
    for row in catalog.licensed_rows():
        sample = row.sample
        dest = staging_path(staging, sample.name)
        if (
            sample.basename.lower() in local
            or (row.local_path and Path(row.local_path).is_file())
            or dest.is_file()
        ):
            report.skipped += 1
            continue
        if limit is not None and report.downloaded >= limit:
            break
        staging.mkdir(exist_ok=True)
        if disk_usage(staging).free < min_free_bytes:
            report.stopped_reason = "low disk space"
            break
        try:
            download_one(client, catalog, row, staging)
        except AuthRejected as exc:
            report.stopped_reason = str(exc)
            break
        except RateLimited as exc:
            report.stopped_reason = f"rate limited: {exc}"
            break
        except SpliceError:
            report.failed.append(sample.catalog_uuid)
            continue
        except OSError as exc:
            report.stopped_reason = f"disk error: {exc.strerror or exc}"
            break
        report.downloaded += 1
    return report
