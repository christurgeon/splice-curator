from datetime import date

import pytest

from splice_curator.config import DEFAULT_SCORING, load_config


def test_autobuy_defaults():
    c = load_config(path=None)
    assert c.keep == 200
    assert c.deadline == date(2026, 11, 6)
    assert c.recent_months == 30 and c.packs_per_genre == 100 and c.pack_pages == 8 and c.discovery_ttl_days == 7
    assert c.max_per_pack_per_group == 6 and c.max_per_pack_total == 25
    assert c.per_label_share == 0.15 and c.max_price_skips == 5
    assert c.max_label_share_total == 0.12
    assert c.scoring == DEFAULT_SCORING and c.scoring is not DEFAULT_SCORING
    assert c.prefer_packs == [] and c.exclude_labels == []


def test_autobuy_keys_load_from_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'keep = 100\ndeadline = 2026-11-10\nprefer_labels = ["Solid Grooves"]\n'
        'exclude_packs = ["Bad Pack"]\n\n[scoring]\nin_pack_pop = 2.0\n'
    )
    c = load_config(path=path)
    assert c.keep == 100 and c.deadline == date(2026, 11, 10)
    assert c.prefer_labels == ["Solid Grooves"] and c.exclude_packs == ["Bad Pack"]
    assert c.scoring["in_pack_pop"] == 2.0 and c.scoring["query_pop"] == 0.6
    assert DEFAULT_SCORING["in_pack_pop"] == 1.0


def test_deadline_as_string_and_bad_scoring_key(tmp_path):
    assert load_config(path=None, overrides={"deadline": "2026-12-01"}).deadline == date(2026, 12, 1)
    with pytest.raises(ValueError, match="scoring"):
        load_config(path=None, overrides={"scoring": {"nope": 1}})
