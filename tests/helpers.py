"""Test helpers. Fake tokens are built at runtime; no JWT literal is stored."""

import base64
import json


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(payload: dict) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    return f"{header}.{body}.{_b64(b'fake-signature-bytes')}"


FSS_NAME = (
    "Serum 2 Melodic House Essentials Volume 5/one_shots/drums/kicks/"
    "FSS_S2MHEV5_Kick_Power.wav"
)


def search_item(
    file_hash: str = "a" * 64,
    catalog_uuid: str = "11111111-2222-4333-8444-555555555555",
    name: str = FSS_NAME,
    pack: str = "Serum 2 Melodic House Essentials Volume 5",
    provider: str = "Freshly Squeezed Samples",
    main_genre: str = "house",
    tags: list[tuple[str, str | None]] | None = None,
    category: str = "oneshot",
    licensed: bool = False,
    bpm: int | None = None,
    key: str | None = None,
    chord_type: str | None = None,
    duration: int | None = 450,
) -> dict:
    """One search item, modeled on the verified assetsSearch response."""
    if tags is None:
        tags = [("kicks", None), ("drums", "Instrument"), ("house", "Genre")]
    return {
        "__typename": "SampleAsset",
        "uuid": file_hash,
        "catalog_uuid": catalog_uuid,
        "name": name,
        "licensed": licensed,
        "asset_category_slug": category,
        "tags": [
            {"label": label, "taxonomy": {"name": tax} if tax else None}
            for label, tax in tags
        ],
        "files": [
            {"asset_file_type_slug": "preview_mp3",
             "url": f"https://spliceproduction.s3.us-west-1.amazonaws.com/audio_samples/{file_hash}/preview.mp3"},
            {"asset_file_type_slug": "waveform",
             "url": f"https://spliceblob.splice.com/waveforms/{file_hash}.json"},
        ],
        "parents": {"items": [
            {"name": pack, "main_genre": main_genre, "provider": {"name": provider}},
        ]},
        "bpm": bpm,
        "key": key,
        "chord_type": chord_type,
        "duration": duration,
        "instrument": None,
    }


def search_response(items: list[dict], records: int = 40448, page: int = 1, pages: int = 809) -> dict:
    return {"data": {"assetsSearch": {
        "response_metadata": {"records": records},
        "pagination_metadata": {"currentPage": page, "totalPages": pages},
        "items": items,
    }}}


def make_sample(i: int = 1, **kw):
    from splice_curator.models import Sample

    base = dict(
        file_hash=f"{i:064x}",
        catalog_uuid=f"00000000-0000-4000-8000-{i:012x}",
        name=f"Pack A/kicks/Kick_{i}.wav",
        pack="Pack A",
        label="Label A",
        pack_genre="house",
        tags=[("kicks", None), ("house", "Genre")],
        bpm=None,
        key=None,
        chord_type=None,
        duration_ms=500,
        category="oneshot",
        licensed=False,
        preview_url=f"https://spliceproduction.s3.us-west-1.amazonaws.com/audio_samples/{i:064x}/p.mp3",
    )
    base.update(kw)
    return Sample(**base)
