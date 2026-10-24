"""Splice GraphQL client: paced, with backoff, and safe downloads.

Every request goes through the shared Throttle. No error message contains
the session token, and download errors omit signed query strings.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from . import queries
from .auth import AuthError, Session
from .config import Config
from .models import Account, AssetFiles, ChartLabel, ChartPack, Pack, PackPage, Sample, SearchPage
from .throttle import Throttle

GRAPHQL_URL = "https://surfaces-graphql.splice.com/graphql"
REFERER = "https://splice.com/sounds/search/samples"
CATEGORIES = ("oneshot", "loop")
SORTS = ("popularity", "relevance", "recency", "random")
ALLOWED_HOST_SUFFIXES = (".splice.com", ".amazonaws.com")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PACK_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")  # legacy pack ids are hex with odd group sizes
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
LOGIN_AGAIN = "Splice rejected the session; log in to splice.com again"


class SpliceError(Exception):
    pass


class AuthRejected(SpliceError):
    pass


class RateLimited(SpliceError):
    pass


class PurchaseUncertain(SpliceError):
    pass


class DownloadRejected(SpliceError):
    pass


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _check_uuid(value: str) -> str:
    if not isinstance(value, str) or not UUID_RE.match(value):
        raise ValueError("catalog_uuid must be a UUID")
    return value


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None  # HTTP-date form: fall back to exponential backoff


def _safe_url(url: httpx.URL | str) -> str:
    u = httpx.URL(str(url))
    return f"{u.scheme}://{u.host}{u.path}"


def check_download_url(url: httpx.URL | str) -> None:
    u = httpx.URL(str(url))
    host = (u.host or "").lower()
    if u.scheme != "https" or not host.endswith(ALLOWED_HOST_SUFFIXES):
        raise DownloadRejected(f"refusing download from {_safe_url(u)}")


def _cookie_header(session: Session) -> str:
    return "; ".join(f"{k}={v}" for k, v in session.cookies.items())


def _parse_sample(item: dict) -> Sample:
    parents = ((item.get("parents") or {}).get("items")) or []
    pack = parents[0] if parents else {}
    provider = pack.get("provider") or {}
    tags = []
    for tag in item.get("tags") or []:
        taxonomy = tag.get("taxonomy") or {}
        tags.append((tag["label"], taxonomy.get("name")))
    preview = next(
        (f["url"] for f in item.get("files") or [] if f.get("asset_file_type_slug") == "preview_mp3"),
        None,
    )
    return Sample(
        file_hash=item["uuid"],
        catalog_uuid=item["catalog_uuid"],
        name=item["name"],
        pack=pack.get("name"),
        label=provider.get("name"),
        pack_genre=pack.get("main_genre"),
        tags=tags,
        bpm=item.get("bpm"),
        key=item.get("key"),
        chord_type=item.get("chord_type"),
        duration_ms=item.get("duration"),
        category=item.get("asset_category_slug") or "",
        licensed=bool(item.get("licensed")),
        preview_url=preview,
        pack_uuid=pack.get("uuid"),
    )


def _check_pack_id(value: str) -> str:
    if not isinstance(value, str) or not PACK_ID_RE.match(value):
        raise ValueError("pack id must be hex")
    return value


def _slug(value: str) -> str:
    if not isinstance(value, str) or not SLUG_RE.match(value):
        raise ValueError("bad permalink")
    return value


def _listing(value) -> list[dict]:
    """A GraphQL list result, whether bare or wrapped in an object."""
    if isinstance(value, dict):
        value = next((v for v in value.values() if isinstance(v, list)), [])
    return [v for v in value or [] if isinstance(v, dict)]


def _opt_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_pack(item: dict) -> Pack:
    counts = item.get("child_asset_counts") or []
    return Pack(
        uuid=item["uuid"],
        name=item.get("name") or "",
        genre=item.get("main_genre"),
        permalink=item.get("permalink_slug"),
        label=(item.get("provider") or {}).get("name"),
        published_at=item.get("published_at"),
        tags=[t["label"] for t in item.get("tags") or [] if t.get("label")],
        sample_count=sum(_opt_int(c.get("count")) or 0 for c in counts if "sample" in str(c.get("type")).lower()),
    )


def _parse_price(prices) -> int | None:
    """Whole credits from an ``asset_prices`` list, or None if absent or odd."""
    for entry in prices or []:
        if not isinstance(entry, dict) or str(entry.get("currency", "")).strip().lower() not in ("credit", "credits"):
            continue
        try:
            amount = float(entry.get("amount"))
        except (TypeError, ValueError):
            return None
        return int(amount) if amount == int(amount) and amount >= 0 else None
    return None


class SpliceClient:
    def __init__(
        self,
        session: Session,
        throttle: Throttle,
        config: Config,
        transport: httpx.BaseTransport | None = None,
        refresher: Callable[[], Session] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session = session
        self.refresher = refresher
        self._clock = clock
        self.throttle = throttle
        self.config = config
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": session.authorization,
            "origin": "https://splice.com",
            "referer": REFERER,
            "user-agent": config.user_agent,
            "x-anonymous-id": config.anonymous_id,
            "cookie": _cookie_header(session),
        }
        self._http = httpx.Client(transport=transport, timeout=30, follow_redirects=False, headers=headers)
        # Downloads go to signed S3 URLs: no Splice token or cookies.
        self._dl = httpx.Client(
            transport=transport,
            timeout=60,
            follow_redirects=True,
            max_redirects=3,
            headers={"user-agent": config.user_agent},
            event_hooks={"request": [lambda r: check_download_url(r.url)]},
        )

    def __repr__(self) -> str:
        return f"SpliceClient({self.session!r})"

    # -- request plumbing -------------------------------------------------

    def _with_retries(self, kind: str, attempt_fn: Callable[[], object]):
        for attempt in range(1, self.config.max_tries + 1):
            self.throttle.wait(kind)
            try:
                return attempt_fn()
            except _Retryable as exc:
                if attempt == self.config.max_tries:
                    raise RateLimited(f"{kind}: {exc}; gave up after {attempt} tries") from None
                self.throttle.backoff(attempt, exc.retry_after)
        raise AssertionError("unreachable")

    def _is_renewal(self, new: Session) -> bool:
        """True if ``new`` outlives the current token and is not about to expire."""
        new_exp, old_exp = new.expires_at, self.session.expires_at
        if new_exp is None or (old_exp is not None and new_exp <= old_exp):
            return False
        return new_exp - self._clock() > self.config.refresh_margin_s

    def _try_refresh(self) -> bool:
        try:
            new = self.refresher()
        except AuthError:
            return False  # Chrome may not have a fresh cookie yet; the caller keeps polling
        if not self._is_renewal(new):
            return False
        self.session = new
        self._http.headers["authorization"] = new.authorization
        self._http.headers["cookie"] = _cookie_header(new)
        stamp = datetime.fromtimestamp(self._clock())
        print(f"[{stamp:%H:%M:%S}] renewed Splice login (expires {datetime.fromtimestamp(new.expires_at):%H:%M})",
              file=sys.stderr, flush=True)
        return True

    def _ensure_fresh(self, force: bool = False) -> None:
        """Make sure the GraphQL token is valid; pick up a renewed one from Chrome if not."""
        exp = self.session.expires_at
        if not force and exp is not None and exp - self._clock() > self.config.refresh_margin_s:
            return
        if self.refresher is None:
            raise AuthRejected("token expires soon; log in to splice.com again")
        if self._try_refresh():
            return
        if force and exp is not None and exp - self._clock() > self.config.refresh_margin_s:
            # A 401 on an unexpired token is a server-side rejection; waiting for Chrome will not help.
            raise AuthRejected(LOGIN_AGAIN)
        cfg = self.config
        print(f"waiting for Chrome to renew the Splice login — keep a splice.com tab open "
              f"(up to {cfg.refresh_wait_s / 60:g} min)", file=sys.stderr, flush=True)
        waited = 0.0
        while waited < cfg.refresh_wait_s:
            step = min(cfg.refresh_poll_s, cfg.refresh_wait_s - waited)
            self.throttle.sleep(step)
            waited += step
            if self._try_refresh():
                return
        raise AuthRejected("Splice login expired and Chrome did not renew it; open splice.com in Chrome and run again")

    def _post_once(self, operation: str, query: str) -> httpx.Response:
        self._ensure_fresh()
        return self._http.post(GRAPHQL_URL, content=json.dumps({"operationName": operation, "query": query}))

    @staticmethod
    def _data(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            raise SpliceError(f"response was not JSON (HTTP {response.status_code})") from None
        errors = body.get("errors") or []
        messages = "; ".join(str(e.get("message", ""))[:200] for e in errors)
        codes = {str((e.get("extensions") or {}).get("code", "")).upper() for e in errors}
        if codes & {"UNAUTHENTICATED", "FORBIDDEN"}:
            raise AuthRejected(LOGIN_AGAIN)
        if not body.get("data"):
            raise SpliceError(f"GraphQL error: {messages or 'no data'}")
        return body["data"]

    def _graphql(self, kind: str, operation: str, query: str) -> dict:
        renewed = False

        def post() -> httpx.Response:
            try:
                return self._post_once(operation, query)
            except httpx.TransportError as exc:
                raise _Retryable(f"network error ({type(exc).__name__})") from None

        def attempt():
            nonlocal renewed
            r = post()
            if r.status_code in (401, 403) and self.refresher is not None and not renewed:
                renewed = True  # one forced renewal per call, then one retry
                self._ensure_fresh(force=True)
                self.throttle.wait(kind)
                r = post()
            if r.status_code in (401, 403):
                raise AuthRejected(LOGIN_AGAIN)
            if r.status_code == 429 or r.status_code >= 500:
                raise _Retryable(f"HTTP {r.status_code}", _retry_after(r))
            if r.status_code != 200:
                raise SpliceError(f"{kind}: HTTP {r.status_code}")
            return self._data(r)

        return self._with_retries(kind, attempt)

    # -- API calls ----------------------------------------------------------

    def account(self) -> Account:
        data = self._graphql("account", "Account", queries.ACCOUNT)
        user = data.get("user") or {}
        sub = data.get("subscription") or {}
        return Account(username=user.get("username") or "", credits=sub.get("credits"), state=sub.get("state") or "")

    def search(
        self,
        query: str | None,
        category: str | None,
        page: int,
        limit: int = 50,
        sort: str = "popularity",
        licensed: bool | None = None,
        pack_uuid: str | None = None,
    ) -> SearchPage:
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        if sort not in SORTS:
            raise ValueError(f"sort must be one of {SORTS}")
        page, limit = int(page), int(limit)
        if page < 1 or not 1 <= limit <= 100:
            raise ValueError("bad page or limit")
        extra = ""
        if query:
            extra += f", query: {json.dumps(query)}"
        if category:
            extra += f", asset_category_slug: {category}"
        if licensed is not None:
            extra += f", licensed: {'true' if licensed else 'false'}"
        children = f', children: {{parent_asset_uuid: "{_check_pack_id(pack_uuid)}"}}' if pack_uuid else ""
        doc = queries.SEARCH.substitute(filter=extra, children=children, page=page, limit=limit, sort=sort)
        data = self._graphql("search", "SamplesSearch", doc)
        result = data.get("assetsSearch") or {}
        samples = [_parse_sample(i) for i in result.get("items") or [] if i and i.get("catalog_uuid")]
        return SearchPage(
            total_records=int((result.get("response_metadata") or {}).get("records") or 0),
            total_pages=int((result.get("pagination_metadata") or {}).get("totalPages") or 0),
            samples=samples,
        )

    def pack_samples(self, pack_uuid: str, category: str, page: int, limit: int = 100) -> SearchPage:
        """Samples inside one pack, most popular first."""
        return self.search(None, category, page, limit=limit, pack_uuid=pack_uuid)

    def pack_search(self, query: str | None, page: int, limit: int = 50) -> PackPage:
        """Packs, most popular first."""
        page, limit = int(page), int(limit)
        if page < 1 or not 1 <= limit <= 100:
            raise ValueError("bad page or limit")
        extra = f", query: {json.dumps(query)}" if query else ""
        doc = queries.PACK_SEARCH.substitute(filter=extra, page=page, limit=limit)
        result = self._graphql("search", "PackSearch", doc).get("assetsSearch") or {}
        packs = [_parse_pack(i) for i in result.get("items") or [] if i and i.get("uuid")]
        return PackPage(
            total_records=int((result.get("response_metadata") or {}).get("records") or 0),
            total_pages=int((result.get("pagination_metadata") or {}).get("totalPages") or 0),
            packs=packs,
        )

    def packs_chart(self, per_page: int = 50, provider_permalink: str | None = None) -> list[ChartPack]:
        """Weekly top packs, globally or for one label."""
        extra = f", providerPermalink: {json.dumps(_slug(provider_permalink))}" if provider_permalink else ""
        doc = queries.PACKS_CHART.substitute(per_page=int(per_page), provider=extra)
        items = _listing(self._graphql("search", "PacksChart", doc).get("legacyPacksChart"))
        scope = provider_permalink or "global"
        charted = []
        for item in items:
            pos = _opt_int(item.get("current_global_chart_pos"))
            if item.get("uuid") and pos is not None:
                charted.append(ChartPack(
                    uuid=item["uuid"], name=item.get("name") or "",
                    label=(item.get("provider") or {}).get("name"),
                    position=pos, last_position=_opt_int(item.get("last_global_chart_pos")), scope=scope,
                ))
        return charted

    def labels_chart(self, per_page: int = 50) -> list[ChartLabel]:
        """Weekly top labels."""
        doc = queries.LABELS_CHART.substitute(per_page=int(per_page))
        items = _listing(self._graphql("search", "LabelsChart", doc).get("wwwPremiumProviders"))
        charted = []
        for item in items:
            pos = _opt_int(item.get("current_provider_chart_position"))
            if item.get("uuid") and pos is not None:
                charted.append(ChartLabel(
                    uuid=item["uuid"], name=item.get("name") or "", permalink=item.get("permalink_slug"),
                    position=pos, last_position=_opt_int(item.get("last_provider_chart_position")),
                    pack_count=_opt_int(item.get("pack_count")),
                    tags=[str(t) for t in item.get("tags_array") or []],
                ))
        return charted

    def asset_files(self, catalog_uuid: str) -> AssetFiles:
        doc = queries.ASSET_FILES.substitute(uuid=_check_uuid(catalog_uuid))
        asset = self._graphql("asset", "AssetFiles", doc).get("asset")
        if not asset or asset.get("__typename") != "SampleAsset":
            raise SpliceError(f"asset {catalog_uuid} not found")
        files = {f.get("asset_file_type_slug"): f.get("url") for f in asset.get("files") or []}
        return AssetFiles(
            name=asset.get("name") or "",
            licensed=bool(asset.get("licensed")),
            preview_url=files.get("preview_mp3"),
            source_url=files.get("source"),
            price=_parse_price(asset.get("asset_prices")),
        )

    def purchase(self, catalog_uuid: str) -> bool:
        """Spend one credit. Never retried: any doubt raises PurchaseUncertain."""
        doc = queries.PURCHASE.substitute(uuid=_check_uuid(catalog_uuid))
        self.throttle.wait("purchase")
        try:
            r = self._post_once("PurchaseAsset", doc)
        except httpx.TransportError as exc:
            raise PurchaseUncertain(f"purchase {catalog_uuid}: network error ({type(exc).__name__})") from None
        if r.status_code in (401, 403):
            raise AuthRejected(LOGIN_AGAIN)
        if r.status_code != 200:
            raise PurchaseUncertain(f"purchase {catalog_uuid}: HTTP {r.status_code}")
        try:
            data = self._data(r)
        except AuthRejected:
            raise
        except SpliceError as exc:
            raise PurchaseUncertain(f"purchase {catalog_uuid}: {exc}") from None
        result = data.get("purchaseAsset")
        if not isinstance(result, dict) or "licensed" not in result:
            raise PurchaseUncertain(f"purchase {catalog_uuid}: unexpected response")
        return bool(result["licensed"])

    def download(self, url: str, dest: Path) -> int:
        """Stream ``url`` to ``dest`` atomically; return bytes written."""
        check_download_url(url)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name("." + dest.name + ".partial")

        def attempt():
            written = 0
            try:
                with self._dl.stream("GET", url) as r:
                    check_download_url(r.url)
                    if r.status_code == 429 or r.status_code >= 500:
                        raise _Retryable(f"HTTP {r.status_code}", _retry_after(r))
                    if r.status_code in (401, 403):
                        raise SpliceError(f"download refused (HTTP {r.status_code}); the signed URL may have expired")
                    if r.status_code != 200:
                        raise SpliceError(f"download failed: HTTP {r.status_code} from {_safe_url(r.url)}")
                    with open(partial, "wb") as fh:
                        for chunk in r.iter_bytes():
                            fh.write(chunk)
                            written += len(chunk)
                        fh.flush()
                        os.fsync(fh.fileno())
                os.replace(partial, dest)
                return written
            except httpx.TooManyRedirects:
                raise DownloadRejected("too many redirects") from None
            except httpx.TransportError as exc:
                raise SpliceError(f"download interrupted ({type(exc).__name__})") from None
            finally:
                partial.unlink(missing_ok=True)

        return self._with_retries("download", attempt)

    def close(self) -> None:
        self._http.close()
        self._dl.close()
