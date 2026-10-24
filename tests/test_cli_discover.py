from splice_curator import cli
from splice_curator.catalog import Catalog
from tests.test_discover import FakeDiscoveryClient, make_pack, pack_sample


def test_discover_command(tmp_path, cli_env, monkeypatch, capsys):
    pack = make_pack(1)
    client = FakeDiscoveryClient({"tech house": [pack]}, {pack.uuid: [pack_sample(1, pack, "Kick_A.wav")]})
    monkeypatch.setattr(cli, "build_client", lambda config, source: client)
    assert cli.main(["--config", str(cli_env), "discover", "--genre", "tech_house"]) == 0
    out = capsys.readouterr().out
    assert "tech_house: 1 packs" in out and "sounds recorded 1" in out and "pack_samples 2" in out
    cat = Catalog(tmp_path / "data" / "catalog.sqlite")
    assert len(cat.rows_for_group("kicks", "tech_house")) == 1


def test_discover_unknown_genre(cli_env, capsys):
    assert cli.main(["--config", str(cli_env), "discover", "--genre", "nope"]) == 2
