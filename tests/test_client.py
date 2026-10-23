import json

import httpx
import pytest

from splice_curator.client import (
    AuthRejected,
    DownloadRejected,
    PurchaseUncertain,
    RateLimited,
    SpliceClient,
    SpliceError,
)
from tests.helpers import search_item, search_response

UUID = "11111111-2222-4333-8444-555555555555"
SOURCE_URL = "https://spliceproduction.s3.us-west-1.amazonaws.com/audio_samples/abc?X-Amz-Signature=s"


def make_client(session, throttle, config, handler):
    return SpliceClient(session, throttle, config, transport=httpx.MockTransport(handler))


def asset_response(licensed=True, source=SOURCE_URL):
    files = [{"asset_file_type_slug": "preview_mp3", "url": "https://x.s3.amazonaws.com/p.mp3"}]
    if source:
        files.append({"asset_file_type_slug": "source", "url": source})
    return {"data": {"asset": {"__typename": "SampleAsset", "name": "Pack/Kick.wav",
                               "licensed": licensed, "files": files}}}


def test_sends_captured_headers_and_cookies(session, throttle, config, token):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {
            "user": {"username": "u"}, "subscription": {"credits": 3360, "state": "active"}}})

    make_client(session, throttle, config, handler).account()
    r = seen[0]
    assert str(r.url) == "https://surfaces-graphql.splice.com/graphql"
    assert r.method == "POST"
    assert r.headers["authorization"] == "Bearer " + token
    assert r.headers["accept"] == "application/json"
    assert r.headers["content-type"] == "application/json"
    assert r.headers["origin"] == "https://splice.com"
    assert r.headers["referer"].startswith("https://splice.com/")
    assert r.headers["user-agent"] == config.user_agent
    assert r.headers["x-anonymous-id"] == config.anonymous_id
    for name in session.cookies:
        assert name + "=" in r.headers["cookie"]
    body = json.loads(r.content)
    assert "query" in body


def test_account_parses(session, throttle, config):
    def handler(request):
        return httpx.Response(200, json={"data": {
            "user": {"username": "u"}, "subscription": {"credits": 3360, "state": "active"}}})

    acc = make_client(session, throttle, config, handler).account()
    assert (acc.username, acc.credits, acc.state) == ("u", 3360, "active")


def test_search_parses_sample(session, throttle, config):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=search_response([search_item()]))

    page = make_client(session, throttle, config, handler).search("house kick", "oneshot", 2)
    assert page.total_records == 40448 and page.total_pages == 809
    s = page.samples[0]
    assert s.file_hash == "a" * 64
    assert s.catalog_uuid == UUID
    assert s.basename == "FSS_S2MHEV5_Kick_Power.wav"
    assert s.pack == "Serum 2 Melodic House Essentials Volume 5"
    assert s.label == "Freshly Squeezed Samples"
    assert s.pack_genre == "house"
    assert s.category == "oneshot"
    assert ("kicks", None) in s.tags
    assert ("drums", "Instrument") in s.tags
    assert s.preview_url.endswith("preview.mp3")
    assert s.duration_ms == 450
    q = bodies[0]["query"]
    assert bodies[0]["operationName"] == "SamplesSearch"
    assert 'query: "house kick"' in q
    assert "asset_category_slug: oneshot" in q
    assert "page: 2" in q and "limit: 50" in q and "sort: popularity" in q
    assert "licensed:" not in q.split("pagination")[0]


def test_search_licensed_filter_and_escaping(session, throttle, config):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content)["query"])
        return httpx.Response(200, json=search_response([]))

    c = make_client(session, throttle, config, handler)
    c.search('a "b"\\', None, 1, licensed=True)
    assert "licensed: true" in bodies[0]
    assert r'query: "a \"b\"\\"' in bodies[0]
    with pytest.raises(ValueError):
        c.search(None, "pack) { evil", 1)
    with pytest.raises(ValueError):
        c.search(None, None, 1, sort="evil")


def test_every_call_goes_through_throttle(session, throttle, config, tmp_path):
    def handler(request):
        if request.url.host == "surfaces-graphql.splice.com":
            q = json.loads(request.content)["query"]
            if "purchaseAsset" in q:
                return httpx.Response(200, json={"data": {"purchaseAsset": {"uuid": UUID, "licensed": True}}})
            if "AssetFiles" in q:
                return httpx.Response(200, json=asset_response())
            if "assetsSearch" in q:
                return httpx.Response(200, json=search_response([]))
            return httpx.Response(200, json={"data": {
                "user": {"username": "u"}, "subscription": {"credits": 1, "state": "active"}}})
        return httpx.Response(200, content=b"RIFF")

    c = make_client(session, throttle, config, handler)
    c.search("x", None, 1)
    c.account()
    c.asset_files(UUID)
    c.purchase(UUID)
    c.download(SOURCE_URL, tmp_path / "a.wav")
    assert throttle.waits == ["search", "account", "asset", "purchase", "download"]


def test_401_raises_auth_rejected(session, throttle, config):
    c = make_client(session, throttle, config, lambda r: httpx.Response(401))
    with pytest.raises(AuthRejected) as exc:
        c.account()
    assert "log in to splice.com again" in str(exc.value)
    c = make_client(session, throttle, config, lambda r: httpx.Response(403))
    with pytest.raises(AuthRejected):
        c.search("x", None, 1)


def test_429_retries_with_retry_after_then_succeeds(session, throttle, config):
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json=search_response([search_item()])),
    ]
    c = make_client(session, throttle, config, lambda r: responses.pop(0))
    page = c.search("x", None, 1)
    assert len(page.samples) == 1
    assert throttle.backoffs == [(1, 7.0)]


def test_5xx_gives_up_after_max_tries(session, throttle, config):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    c = make_client(session, throttle, config, handler)
    with pytest.raises(RateLimited):
        c.search("x", None, 1)
    assert len(calls) == config.max_tries == 4
    assert [a for a, _ in throttle.backoffs] == [1, 2, 3]


def test_purchase_is_not_retried(session, throttle, config):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    c = make_client(session, throttle, config, handler)
    with pytest.raises(PurchaseUncertain):
        c.purchase(UUID)
    assert len(calls) == 1
    assert throttle.backoffs == []


def test_purchase_network_error_is_uncertain(session, throttle, config):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(PurchaseUncertain):
        make_client(session, throttle, config, handler).purchase(UUID)
    assert len(calls) == 1


def test_purchase_returns_licensed_flag(session, throttle, config):
    def ok(request):
        assert f'purchaseAsset(uuid: "{UUID}")' in json.loads(request.content)["query"]
        return httpx.Response(200, json={"data": {"purchaseAsset": {"uuid": UUID, "licensed": True}}})

    assert make_client(session, throttle, config, ok).purchase(UUID) is True
    no = lambda r: httpx.Response(200, json={"data": {"purchaseAsset": {"uuid": UUID, "licensed": False}}})  # noqa: E731
    assert make_client(session, throttle, config, no).purchase(UUID) is False
    err = lambda r: httpx.Response(200, json={"errors": [{"message": "nope"}]})  # noqa: E731
    with pytest.raises(PurchaseUncertain):
        make_client(session, throttle, config, err).purchase(UUID)


def test_purchase_rejects_non_uuid(session, throttle, config):
    c = make_client(session, throttle, config, lambda r: httpx.Response(500))
    with pytest.raises(ValueError):
        c.purchase('x") { evil }')
    assert throttle.waits == []


def test_asset_files_parses_source(session, throttle, config):
    c = make_client(session, throttle, config, lambda r: httpx.Response(200, json=asset_response()))
    af = c.asset_files(UUID)
    assert af.licensed is True
    assert af.source_url == SOURCE_URL
    assert af.preview_url.endswith("p.mp3")
    assert af.name == "Pack/Kick.wav"
    assert "Signature" not in repr(af)


def test_graphql_errors_without_data_raise(session, throttle, config):
    c = make_client(session, throttle, config,
                    lambda r: httpx.Response(200, json={"errors": [{"message": "bad field"}]}))
    with pytest.raises(SpliceError) as exc:
        c.account()
    assert "bad field" in str(exc.value)


def test_download_rejects_foreign_host(session, throttle, config, tmp_path):
    calls = []
    c = make_client(session, throttle, config, lambda r: calls.append(1) or httpx.Response(200))
    dest = tmp_path / "a.wav"
    for url in ("https://evil.example.com/a.wav",
                "http://spliceproduction.s3.amazonaws.com/a",
                "https://amazonaws.com.evil.com/a",
                "https://notsplice.com/a"):
        with pytest.raises(DownloadRejected):
            c.download(url, dest)
    assert not dest.exists()
    assert calls == []


def test_download_rejects_redirect_to_foreign_host(session, throttle, config, tmp_path):
    def handler(request):
        if request.url.host.endswith("amazonaws.com"):
            return httpx.Response(302, headers={"Location": "https://evil.example.com/x"})
        return httpx.Response(200, content=b"evil")

    with pytest.raises(DownloadRejected):
        make_client(session, throttle, config, handler).download(SOURCE_URL, tmp_path / "a.wav")
    assert list(tmp_path.iterdir()) == []


def test_download_does_not_send_splice_credentials(session, throttle, config, tmp_path, token):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"RIFF")

    make_client(session, throttle, config, handler).download(SOURCE_URL, tmp_path / "a.wav")
    assert "authorization" not in seen[0].headers
    assert "cookie" not in seen[0].headers


def test_download_writes_atomically(session, throttle, config, tmp_path):
    c = make_client(session, throttle, config, lambda r: httpx.Response(200, content=b"RIFF" * 1000))
    dest = tmp_path / "Pack" / "sub" / "Kick.wav"
    n = c.download(SOURCE_URL, dest)
    assert n == 4000
    assert dest.read_bytes() == b"RIFF" * 1000
    assert [p.name for p in dest.parent.iterdir()] == ["Kick.wav"]


class _BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"RIFF" * 100
        raise httpx.ReadError("connection dropped")


def test_download_failure_removes_partial(session, throttle, config, tmp_path):
    c = make_client(session, throttle, config, lambda r: httpx.Response(200, stream=_BrokenStream()))
    dest = tmp_path / "Kick.wav"
    with pytest.raises(SpliceError):
        c.download(SOURCE_URL, dest)
    assert list(tmp_path.iterdir()) == []


def test_errors_never_contain_token(session, throttle, config, token, tmp_path):
    errors = []
    cases = [
        (lambda r: httpx.Response(401), lambda c: c.account()),
        (lambda r: httpx.Response(503), lambda c: c.account()),
        (lambda r: httpx.Response(503), lambda c: c.purchase(UUID)),
        (lambda r: httpx.Response(200, json={"errors": [{"message": "x"}]}), lambda c: c.account()),
        (lambda r: httpx.Response(200), lambda c: c.download("https://evil.example.com/a", tmp_path / "a")),
    ]
    for handler, call in cases:
        with pytest.raises(SpliceError) as exc:
            call(make_client(session, throttle, config, handler))
        errors.append(exc.value)
    assert {type(e) for e in errors} == {AuthRejected, RateLimited, PurchaseUncertain, SpliceError, DownloadRejected}
    for e in errors:
        assert token not in str(e) and token not in repr(e)
