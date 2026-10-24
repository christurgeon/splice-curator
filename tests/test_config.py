import tomllib

from splice_curator.config import Config, Delay, load_config


def test_defaults_match_spec():
    c = load_config(path=None)
    assert c.delays["search"] == Delay(3, 6)
    assert c.delays["account"] == Delay(3, 6)
    assert c.delays["asset"] == Delay(3, 6)
    assert c.delays["purchase"] == Delay(6, 12)
    assert c.delays["download"] == Delay(2, 5)
    assert c.max_purchases_per_day == 400
    assert c.max_tries == 4
    assert c.min_free_bytes == 2 * 1024**3
    assert "Chrome/" in c.user_agent


def test_toml_then_overrides(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "max_purchases_per_day = 100\n\n[delays.purchase]\nmin_s = 10\nmax_s = 20\n"
    )
    c = load_config(path=path, overrides={"max_purchases_per_day": 50})
    assert c.delays["purchase"] == Delay(10, 20)
    assert c.delays["search"] == Delay(3, 6)
    assert c.max_purchases_per_day == 50


def test_override_delays(tmp_path):
    c = load_config(path=tmp_path / "c.toml", overrides={"delays": {"search": Delay(1, 2)}})
    assert c.delays["search"] == Delay(1, 2)
    assert c.delays["purchase"] == Delay(6, 12)


def test_anonymous_id_generated_once_and_persisted(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("per_pack_cap = 3\n")
    first = load_config(path=path)
    assert first.anonymous_id
    data = tomllib.loads(path.read_text())
    assert data["anonymous_id"] == first.anonymous_id
    assert data["per_pack_cap"] == 3
    assert load_config(path=path).anonymous_id == first.anonymous_id


def test_paths_expand_and_catalog_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path / "data"}"\nstaging_dir = "~/stage"\n')
    c = load_config(path=path)
    assert isinstance(c, Config)
    assert c.catalog_path == tmp_path / "data" / "catalog.sqlite"
    assert "~" not in str(c.staging_dir)


def test_config_genre_override_and_disable(tmp_path):
    from splice_curator.budget import genre_quotas, get_group

    path = tmp_path / "config.toml"
    path.write_text(
        "[genres.afro_house]\nweight = 0\n\n[genres.tech_house]\nweight = 0.50\n"
    )
    c = load_config(path=path)
    assert [g.id for g in c.genres] == ["tech_house", "minimal", "deep_house", "classic_house"]
    assert c.genres[0].prefixes == ("tech house",)  # unset keys keep the default
    assert genre_quotas(get_group("kicks"), c.genres) == {
        "tech_house": 150, "minimal": 75, "deep_house": 45, "classic_house": 30,
    }


def test_config_genre_weights_must_sum_to_one(tmp_path):
    import pytest

    path = tmp_path / "config.toml"
    path.write_text("[genres.tech_house]\nweight = 0.50\n")
    with pytest.raises(ValueError, match="sum to 1.0.*1.1"):
        load_config(path=path)
    path.write_text("[genres.new]\nweight = 0\n")
    with pytest.raises(ValueError, match="needs keys"):
        load_config(path=path)
