import json

import httpx
import pytest

from tests.helpers import search_item, search_response
from tests.test_client import make_client

PACK = "aaaaaaaa-1111-4222-8333-444444444444"


def pack_item(uuid=PACK, name="Tech House Heat", **kw):
    item = {
        "uuid": uuid, "name": name, "main_genre": "tech house", "permalink_slug": "tech-house-heat",
        "published_at": "2026-05-01T00:00:00Z", "provider": {"name": "Solid Grooves"},
        "child_asset_counts": [{"type": "sample", "count": 80}, {"type": "preset", "count": 3}],
        "tags": [{"label": "groovy"}, {"label": "tech house"}],
    }
    item.update(kw)
    return item


def capture(body):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    return seen, handler


def test_pack_search_parses_packs(session, throttle, config):
    body = {"data": {"assetsSearch": {
        "response_metadata": {"records": 3080}, "pagination_metadata": {"currentPage": 1, "totalPages": 62},
        "items": [pack_item(), {}],
    }}}
    seen, handler = capture(body)
    page = make_client(session, throttle, config, handler).pack_search("tech house", 2)
    assert page.total_records == 3080 and page.total_pages == 62
    (p,) = page.packs
    assert (p.uuid, p.name, p.genre, p.label) == (PACK, "Tech House Heat", "tech house", "Solid Grooves")
    assert p.tags == ["groovy", "tech house"]
    assert p.sample_count == 80 and p.published_at.startswith("2026-05")
    q = seen[0]["query"]
    # legacy ids are the ones `children` and the charts understand
    assert "legacy: true, published: true, asset_type_slug: pack" in q and "legacy: {use: true}" in q
    assert "asset_type_slug: pack" in q and 'query: "tech house"' in q
    assert "sort: popularity" in q and "page: 2" in q


def test_pack_samples_filters_by_parent_and_category(session, throttle, config):
    seen, handler = capture(search_response([search_item()]))
    page = make_client(session, throttle, config, handler).pack_samples(PACK, "loop", 1, limit=100)
    assert len(page.samples) == 1
    q = seen[0]["query"]
    assert f'parent_asset_uuid: "{PACK}"' in q
    assert "asset_category_slug: loop}, children:" in q  # `children` is a sibling of `filter`
    assert "limit: 100" in q and "sort: popularity" in q


def test_pack_samples_rejects_bad_uuid(session, throttle, config):
    c = make_client(session, throttle, config, lambda r: httpx.Response(500))
    with pytest.raises(ValueError):
        c.pack_samples('x"}) { evil', "loop", 1)


def test_search_parses_pack_uuid(session, throttle, config):
    item = search_item()
    item["parents"]["items"][0]["uuid"] = PACK
    _, handler = capture(search_response([item]))
    sample = make_client(session, throttle, config, handler).search("k", None, 1).samples[0]
    assert sample.pack_uuid == PACK


def test_packs_chart_global_and_label(session, throttle, config):
    body = {"data": {"legacyPacksChart": [
        {"uuid": PACK, "name": "Tech House Heat", "permalink_slug": "x", "current_global_chart_pos": 3,
         "last_global_chart_pos": None, "provider": {"uuid": "p", "name": "Solid Grooves", "permalink_slug": "sg"},
         "child_asset_counts": [{"count": 60, "type": "sample"}]},
        {"uuid": "bbbbbbbb-1111-4222-8333-444444444444", "name": "No position", "current_global_chart_pos": None},
    ]}}
    seen, handler = capture(body)
    c = make_client(session, throttle, config, handler)
    charted = c.packs_chart(50)
    assert [(x.uuid, x.position, x.last_position, x.scope, x.label) for x in charted] == [
        (PACK, 3, None, "global", "Solid Grooves")]
    assert "perPage: 50" in seen[0]["query"] and "providerPermalink" not in seen[0]["query"]
    c.packs_chart(50, provider_permalink="solid-grooves")
    assert 'providerPermalink: "solid-grooves"' in seen[1]["query"]
    with pytest.raises(ValueError):
        c.packs_chart(50, provider_permalink='a"}) { evil')


def test_labels_chart(session, throttle, config):
    body = {"data": {"wwwPremiumProviders": [
        {"uuid": "l1", "legacy_uuid": "9", "name": "Solid Grooves", "permalink_slug": "solid-grooves",
         "current_provider_chart_position": 2, "last_provider_chart_position": 5, "pack_count": 120,
         "tags_array": ["tech house", "house"]},
    ]}}
    seen, handler = capture(body)
    (label,) = make_client(session, throttle, config, handler).labels_chart(50)
    assert (label.name, label.position, label.last_position, label.pack_count) == ("Solid Grooves", 2, 5, 120)
    assert label.tags == ["tech house", "house"]
    assert 'sort: "chart"' in seen[0]["query"] and "per_page: 50" in seen[0]["query"]


def test_chart_accepts_wrapped_list(session, throttle, config):
    body = {"data": {"wwwPremiumProviders": {"items": [
        {"uuid": "l1", "name": "A", "current_provider_chart_position": 1}]}}}
    _, handler = capture(body)
    assert len(make_client(session, throttle, config, handler).labels_chart()) == 1


def test_pack_samples_accepts_legacy_pack_id(session, throttle, config):
    seen, handler = capture(search_response([]))
    make_client(session, throttle, config, handler).pack_samples("796e02ba-0b2e-8680-7860-b3387cec28998dfb579", "loop", 1)
    assert 'parent_asset_uuid: "796e02ba-0b2e-8680-7860-b3387cec28998dfb579"' in seen[0]["query"]
