import pytest

from splice_curator.budget import (
    GENRES, GROUPS, RESERVE, TARGET_GENRES, genre_quotas, get_genre, get_group, subtype_quotas,
)


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


def test_genre_weights_sum_to_one():
    assert abs(sum(g.weight for g in GENRES) - 1.0) < 1e-9
    with pytest.raises(KeyError):
        get_genre("nope")


def test_genre_quotas_sum_exactly():
    for g in GROUPS:
        assert sum(genre_quotas(g).values()) == g.credits
    assert genre_quotas(get_group("kicks")) == {
        "tech_house": 120, "minimal": 75, "deep_house": 45, "afro_house": 30, "classic_house": 30,
    }


def test_subtype_quotas_even_split():
    hats = get_group("hats_cymbals")
    assert subtype_quotas(hats, 200) == {"closed hat": 67, "open hat": 67, "ride": 66}
    assert subtype_quotas(hats, 2) == {"closed hat": 1, "open hat": 1, "ride": 0}
    assert sum(subtype_quotas(get_group("perc_oneshots"), 47).values()) == 47
