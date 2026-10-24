import json

import pytest

from splice_curator.budget import get_group
from splice_curator.review import load_approved, write_review_page
from tests.helpers import make_sample


def test_review_page_contains_rows_and_no_token(tmp_path):
    samples = [make_sample(i) for i in range(1, 4)]
    samples.append(make_sample(9, name="P/</script><b>x</b>.wav", pack="P & <Q>"))
    path = write_review_page(samples, get_group("kicks"), "kicks-20261023-1200", tmp_path / "review")
    assert path == tmp_path / "review" / "kicks-20261023-1200.html"
    html = path.read_text()
    for s in samples:
        assert s.catalog_uuid in html
        assert f"https://splice.com/sounds/sample/{s.file_hash}" in html
        # Splice scrambles preview MP3s; the page links to splice.com instead of playing them.
        assert s.preview_url not in html
    assert "Bearer" not in html and "eyJ" not in html
    assert "</script><b>" not in html
    assert "audio" not in html.lower()
    assert "approved-" in html
    assert "<script src" not in html


def test_load_approved_roundtrip_and_validation(tmp_path):
    u1 = make_sample(1).catalog_uuid
    u2 = make_sample(2).catalog_uuid
    good = tmp_path / "a.json"
    good.write_text(json.dumps({"batch_id": "kicks-1", "group": "kicks", "catalog_uuids": [u1, u2, u1]}))
    assert load_approved(good) == ("kicks-1", "kicks", [u1, u2])

    bad_cases = [
        {"batch_id": "kicks-1", "group": "kicks", "catalog_uuids": ["not-a-uuid"]},
        {"batch_id": "kicks-1", "group": "kicks", "catalog_uuids": u1},
        {"batch_id": "", "group": "kicks", "catalog_uuids": [u1]},
        {"group": "kicks", "catalog_uuids": [u1]},
        {"batch_id": "kicks-1", "group": "nope", "catalog_uuids": [u1]},
        [u1],
    ]
    for case in bad_cases:
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(case))
        with pytest.raises(ValueError):
            load_approved(p)
    p = tmp_path / "broken.json"
    p.write_text("{")
    with pytest.raises(ValueError):
        load_approved(p)


def test_shortlist_command_writes_page_and_marks_rows(tmp_path, fake_client, cli_env, capsys):
    from splice_curator import cli
    from splice_curator.catalog import Catalog

    assert cli.main(["--config", str(cli_env), "search", "--group", "kicks", "--pages", "3", "--query", "house kick"]) == 0
    capsys.readouterr()
    fake_client.calls.clear()
    assert cli.main(["--config", str(cli_env), "shortlist", "--group", "kicks", "--size", "4"]) == 0
    out = capsys.readouterr().out
    assert fake_client.calls == []  # shortlist is offline
    pages = list((tmp_path / "data" / "review").glob("kicks-*.html"))
    assert len(pages) == 1 and str(pages[0]) in out
    assert "4 credits" in out
    rows = Catalog(tmp_path / "data" / "catalog.sqlite").rows_for_group("kicks")
    assert sum(1 for r in rows if r.shortlisted_batch) == 4
    # second run picks only what is left
    assert cli.main(["--config", str(cli_env), "shortlist", "--group", "kicks", "--size", "4"]) == 0
    assert "2 credits" in capsys.readouterr().out


def _search_genre(cli, cli_env, genre, pages="3"):
    return cli.main(["--config", str(cli_env), "search", "--group", "kicks",
                     "--genre", genre, "--pages", pages])


def test_shortlist_genre_default_size_from_quota(tmp_path, fake_client, cli_env, capsys):
    from splice_curator import cli
    from splice_curator.catalog import Catalog

    # Fake samples have pack_genre "house", so they match only classic_house (quota 30).
    # The fake gives 2 distinct samples per page, but the near-duplicate rule keeps one per pack.
    assert _search_genre(cli, cli_env, "classic_house", pages="1") == 0
    capsys.readouterr()
    assert cli.main(["--config", str(cli_env), "shortlist", "--group", "kicks",
                     "--genre", "classic_house"]) == 0
    assert "credits" in capsys.readouterr().out
    pages = list((tmp_path / "data" / "review").glob("kicks-classic_house-*.html"))
    assert len(pages) == 1
    assert "Classic / jackin house" in pages[0].read_text()
    cat = Catalog(tmp_path / "data" / "catalog.sqlite")
    assert cat.count_spent("kicks", "classic_house") >= 1


def test_shortlist_quota_used_up(tmp_path, fake_client, cli_env, capsys):
    from splice_curator import cli
    from splice_curator.catalog import Catalog
    from tests.helpers import make_sample

    cat = Catalog(tmp_path / "data" / "catalog.sqlite")
    extra = [make_sample(100 + i) for i in range(30)]
    cat.record_search(extra, "q", "kicks", 1, 50, genre="classic_house", subtype="kick")
    cat.mark_shortlisted([s.catalog_uuid for s in extra], "kicks-classic_house-1")
    assert cli.main(["--config", str(cli_env), "shortlist", "--group", "kicks",
                     "--genre", "classic_house"]) == 0
    assert "quota for kicks/classic_house is used up" in capsys.readouterr().out


def test_shortlist_disabled_genre_exits_2(cli_env, capsys):
    from splice_curator import cli

    cli_env.write_text(cli_env.read_text() + "\n[genres.afro_house]\nweight = 0\n\n"
                       "[genres.tech_house]\nweight = 0.50\n")
    assert cli.main(["--config", str(cli_env), "shortlist", "--group", "kicks",
                     "--genre", "afro_house"]) == 2
    assert "unknown genre" in capsys.readouterr().err


def test_quota_command_table(tmp_path, fake_client, cli_env, capsys):
    from splice_curator import cli
    from splice_curator.catalog import Catalog
    from tests.helpers import make_sample

    cat = Catalog(tmp_path / "data" / "catalog.sqlite")
    samples = [make_sample(i) for i in range(1, 5)]
    cat.record_search(samples, "q", "kicks", 1, 50, genre="classic_house", subtype="kick")
    cat.mark_shortlisted([samples[0].catalog_uuid, samples[1].catalog_uuid], "kicks-classic_house-1")
    cat.mark_licensed(samples[1].catalog_uuid)
    cat.mark_licensed(samples[2].catalog_uuid)  # bought outside a genre batch: not counted
    capsys.readouterr()
    assert cli.main(["--config", str(cli_env), "quota", "--group", "kicks"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert fake_client.calls == []
    assert lines[0].split() == ["genre", "quota", "searched", "shortlisted", "bought", "remaining"]
    assert lines[1].split() == ["tech_house", "120", "0", "0", "0", "120"]
    assert lines[-1].split() == ["classic_house", "30", "4", "1", "1", "28"]
    assert len(lines) == 6
