import json
from collections import namedtuple

import httpx

from splice_curator.catalog import Catalog
from splice_curator.client import AuthRejected, SpliceClient
from splice_curator.sync import index_local, refresh_owned, run_sync
from tests.helpers import make_sample

Usage = namedtuple("Usage", "total used free")
PLENTY = lambda p: Usage(10**12, 0, 10**12)  # noqa: E731
LOW = 2 * 1024**3


def owned_setup(tmp_path, fake_client, n=3):
    samples = [
        fake_client.add(make_sample(i, name=f"Pack {i}/drums/Kick:{i}.wav"), owned=True)
        for i in range(1, n + 1)
    ]
    catalog = Catalog(tmp_path / "c.sqlite")
    assert refresh_owned(fake_client, catalog) == n
    return samples, catalog


def test_index_local_skips_asd(tmp_path):
    root = tmp_path / "Splice"
    (root / "pack" / ".hidden").mkdir(parents=True)
    (root / "pack" / "Kick.WAV").write_bytes(b"x")
    (root / "pack" / "Kick.wav.asd").write_bytes(b"x")
    (root / "pack" / "Loop.aif").write_bytes(b"x")
    (root / "pack" / "._Kick.wav").write_bytes(b"x")
    (root / "pack" / ".hidden" / "Secret.wav").write_bytes(b"x")
    (root / "pack" / "notes.txt").write_bytes(b"x")
    idx = index_local([root, tmp_path / "missing"])
    assert set(idx) == {"kick.wav", "loop.aif"}
    assert idx["kick.wav"] == root / "pack" / "Kick.WAV"


def test_refresh_owned_pages(tmp_path, fake_client):
    for i in range(1, 121):
        fake_client.add(make_sample(i), owned=True)
    catalog = Catalog(tmp_path / "c.sqlite")
    assert refresh_owned(fake_client, catalog) == 120
    assert len(catalog.licensed_rows()) == 120
    assert [c[3] for c in fake_client.calls] == [1, 2, 3]
    assert all(c[4] is True for c in fake_client.calls)


def test_refresh_owned_does_not_change_rank(tmp_path, fake_client):
    catalog = Catalog(tmp_path / "c.sqlite")
    s = fake_client.add(make_sample(1), owned=False)
    catalog.record_search([s], "house kick", "kicks", 3, 50)
    fake_client.owned.add(s.catalog_uuid)
    refresh_owned(fake_client, catalog)
    row = catalog.get_by_catalog_uuid(s.catalog_uuid)
    assert row.best_position == 100 and row.sample.licensed


def test_sync_skips_files_already_in_splice_dir(tmp_path, fake_client):
    samples, catalog = owned_setup(tmp_path, fake_client)
    splice = tmp_path / "Splice" / "some" / "folder"
    splice.mkdir(parents=True)
    (splice / samples[0].basename).write_bytes(b"x")
    staging = tmp_path / "staging"
    report = run_sync(fake_client, catalog, staging, [tmp_path / "Splice"], LOW, disk_usage=PLENTY)
    assert (report.downloaded, report.skipped, report.stopped_reason) == (2, 1, None)
    assert fake_client.count("download") == 2
    assert (tmp_path / "Splice" / "some" / "folder" / samples[0].basename).read_bytes() == b"x"


def test_sync_downloads_missing_to_pack_path(tmp_path, fake_client):
    samples, catalog = owned_setup(tmp_path, fake_client, n=1)
    staging = tmp_path / "staging"
    report = run_sync(fake_client, catalog, staging, [], LOW, disk_usage=PLENTY)
    assert report.downloaded == 1
    dest = staging / "Pack 1" / "drums" / "Kick_1.wav"
    assert dest.is_file()
    assert catalog.get_by_catalog_uuid(samples[0].catalog_uuid).local_path == str(dest)


def test_sync_limit(tmp_path, fake_client):
    _, catalog = owned_setup(tmp_path, fake_client)
    report = run_sync(fake_client, catalog, tmp_path / "staging", [], LOW, limit=2, disk_usage=PLENTY)
    assert report.downloaded == 2


def test_sync_stops_when_free_space_low_and_cleans_partial(tmp_path, fake_client, session, throttle, config):
    _, catalog = owned_setup(tmp_path, fake_client)
    report = run_sync(fake_client, catalog, tmp_path / "staging", [], LOW,
                      disk_usage=lambda p: Usage(10**12, 10**12, LOW - 1))
    assert report.stopped_reason == "low disk space"
    assert report.downloaded == 0
    assert fake_client.count("download") == 0
    assert fake_client.count("asset_files") == 0

    # a real client whose download stream breaks mid-way leaves no partial file
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"RIFF" * 1000
            raise httpx.ReadError("drive gone")

    def handler(request):
        if request.url.host == "surfaces-graphql.splice.com":
            uuid = json.loads(request.content)["query"].split('uuid: "')[1].split('"')[0]
            return httpx.Response(200, json={"data": {"asset": {
                "__typename": "SampleAsset", "name": "x", "licensed": True,
                "files": [{"asset_file_type_slug": "source",
                           "url": f"https://spliceproduction.s3.us-west-1.amazonaws.com/audio_samples/{uuid}"}]}}})
        return httpx.Response(200, stream=Broken())

    real = SpliceClient(session, throttle, config, transport=httpx.MockTransport(handler))
    staging = tmp_path / "staging2"
    report = run_sync(real, catalog, staging, [], LOW, limit=1, disk_usage=PLENTY)
    assert report.downloaded == 0
    assert len(report.failed) == 3
    assert [p for p in staging.rglob("*") if p.is_file()] == []


def test_sync_stops_on_disk_error(tmp_path, fake_client):
    _, catalog = owned_setup(tmp_path, fake_client)
    fake_client.download_error = OSError(28, "No space left on device")
    report = run_sync(fake_client, catalog, tmp_path / "staging", [], LOW, disk_usage=PLENTY)
    assert report.stopped_reason.startswith("disk error")
    assert fake_client.count("download") == 1


def test_sync_refuses_missing_drive(tmp_path, fake_client):
    _, catalog = owned_setup(tmp_path, fake_client)
    report = run_sync(fake_client, catalog, tmp_path / "unplugged" / "SpliceCurator" / "downloads",
                      [], LOW, disk_usage=PLENTY)
    assert "missing" in report.stopped_reason
    assert not (tmp_path / "unplugged").exists()


def test_sync_stops_on_auth_rejected(tmp_path, fake_client):
    samples, catalog = owned_setup(tmp_path, fake_client)
    fake_client.errors[("asset_files", samples[1].catalog_uuid)] = AuthRejected("log in to splice.com again")
    report = run_sync(fake_client, catalog, tmp_path / "staging", [], LOW, disk_usage=PLENTY)
    assert report.downloaded == 1
    assert "log in to splice.com again" in report.stopped_reason
    assert fake_client.count("asset_files") == 2


def test_sync_is_resumable(tmp_path, fake_client):
    _, catalog = owned_setup(tmp_path, fake_client)
    staging = tmp_path / "staging"
    first = run_sync(fake_client, catalog, staging, [staging], LOW, disk_usage=PLENTY)
    assert first.downloaded == 3
    fake_client.calls.clear()
    second = run_sync(fake_client, catalog, staging, [staging], LOW, disk_usage=PLENTY)
    assert (second.downloaded, second.skipped) == (0, 3)
    assert fake_client.calls == []


def test_sync_command(tmp_path, fake_client, cli_env, capsys):
    from splice_curator import cli

    for i in range(1, 4):
        fake_client.add(make_sample(i), owned=True)
    (tmp_path / "staging").mkdir()
    cli_env.write_text("min_free_bytes = 0\n" + cli_env.read_text())
    assert cli.main(["--config", str(cli_env), "sync", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "3 owned" in out and "downloaded 2" in out
