import pytest

from splice_curator.budget import GROUPS, RESERVE, TARGET_GENRES, get_group


def test_budget_totals():
    assert sum(g.credits for g in GROUPS) + RESERVE == 3360
    assert sum(g.credits for g in GROUPS if g.category == "oneshot") == 1750
    assert sum(g.credits for g in GROUPS if g.category == "loop") == 1410


def test_group_ids_unique():
    ids = [g.id for g in GROUPS]
    assert len(ids) == len(set(ids)) == 12


def test_groups_have_queries_and_valid_category():
    for g in GROUPS:
        assert g.queries and g.want_tags
        assert g.category in ("oneshot", "loop")


def test_get_group():
    assert get_group("kicks").credits == 300
    assert get_group("kicks").queries == ("house kick", "tech house kick", "deep house kick")
    with pytest.raises(KeyError):
        get_group("nope")


def test_target_genres():
    assert {"house", "deep house", "disco"} <= TARGET_GENRES
