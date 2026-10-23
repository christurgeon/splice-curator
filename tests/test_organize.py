import csv

import pytest

from splice_curator.catalog import Catalog
from splice_curator.organize import classify, format_key, run_organize, target_name
from tests.helpers import make_sample


def tags(*labels):
    return [(label, None) for label in labels]


def test_classify_oneshot_priority():
    s = make_sample(1, tags=tags("drums", "kicks", "house"), category="oneshot")
    assert classify(s) == ("One-Shots", "Kicks")
    assert classify(make_sample(2, tags=tags("percussion", "claps"))) == ("One-Shots", "Snares & Claps")
    assert classify(make_sample(3, tags=tags("synth", "bass"))) == ("One-Shots", "Bass")
    assert classify(make_sample(4, tags=tags("Vocals", "FX"))) == ("One-Shots", "Vocals")


def test_classify_loop_percussion_vs_drums():
    loop = dict(category="loop")
    assert classify(make_sample(1, tags=tags("drums", "percussion", "shakers"), **loop)) == ("Loops", "Percussion")
    assert classify(make_sample(2, tags=tags("drums", "percussion", "kicks"), **loop)) == ("Loops", "Drums")
    assert classify(make_sample(3, tags=tags("tops"), **loop)) == ("Loops", "Drums")
    assert classify(make_sample(4, tags=tags("bass", "drums"), **loop)) == ("Loops", "Bass")
    assert classify(make_sample(5, tags=tags("keys", "vocals"), **loop)) == ("Loops", "Vocals")
    assert classify(make_sample(6, tags=tags("piano"), **loop)) == ("Loops", "Chords & Keys")


def test_classify_unknown_is_other():
    assert classify(make_sample(1, tags=tags("house", "groovy"))) == ("One-Shots", "Other")
    assert classify(make_sample(2, tags=[], category="loop")) == ("Loops", "Other")


def test_format_key():
    assert format_key("a", "minor") == "Am"
    assert format_key("g#", "minor") == "G#m"
    assert format_key("c", "major") == "C"
    assert format_key("c", None) == "C"
    assert format_key(None, "minor") is None
    assert format_key("", None) is None


def test_target_name_is_exfat_safe():
    s = make_sample(1, name="P/a:b?.wav", bpm=124, key="a", chord_type="minor")
    assert target_name(s) == "124_Am_a_b_.wav"
    assert target_name(make_sample(2, name="P/Kick.wav")) == "Kick.wav"
    assert target_name(make_sample(3, name="P/Kick.wav", key="f#")) == "F#_Kick.wav"
    long = target_name(make_sample(4, name="P/" + "x" * 300 + ".wav", bpm=128))
    assert len(long.encode()) <= 255 and long.endswith(".wav")


@pytest.fixture
def lib(tmp_path):
    """Catalog plus a staging and a fake ~/Splice source tree."""
    catalog = Catalog(tmp_path / "c.sqlite")
    staging = tmp_path / "staging"
    splice = tmp_path / "Splice"
    samples = [
        make_sample(1, name="Pack A/kicks/Kick 1.wav", pack="Pack A", tags=[("kicks", None), ("house", "Genre")]),
        make_sample(2, name="Pack B/loops/Groove.wav", pack="Pack B", category="loop", bpm=124,
                    key="g#", chord_type="minor", tags=[("drums", "Instrument"), ("tops", None)]),
    ]
    catalog.record_search(samples, "", None, 1, 50, ranked=False)
    (staging / "Pack A" / "kicks").mkdir(parents=True)
    (staging / "Pack A" / "kicks" / "Kick 1.wav").write_bytes(b"k" * 10)
    (splice / "packs" / "Pack B" / "loops").mkdir(parents=True)
    (splice / "packs" / "Pack B" / "loops" / "Groove.wav").write_bytes(b"g" * 20)
    (splice / "packs" / "Pack B" / "loops" / "Groove.wav.asd").write_bytes(b"a")
    return catalog, staging, splice, samples


def test_organize_copies_and_writes_csv(tmp_path, lib):
    catalog, staging, splice, _ = lib
    target = tmp_path / "drive"
    report = run_organize(catalog, [staging, splice], target)
    assert (report.copied, report.skipped, report.unsorted) == (2, 0, 0)
    kick = target / "Samples" / "One-Shots" / "Kicks" / "Kick 1.wav"
    loop = target / "Samples" / "Loops" / "Drums" / "124_G#m_Groove.wav"
    assert kick.read_bytes() == b"k" * 10
    assert loop.read_bytes() == b"g" * 20
    assert not list(target.rglob("*.asd"))
    with open(target / "Samples" / "catalog.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == ["path", "pack", "label", "genres", "tags", "bpm", "key", "category"]
    by_path = {r["path"]: r for r in rows}
    k = by_path["One-Shots/Kicks/Kick 1.wav"]
    assert (k["pack"], k["label"], k["genres"], k["category"]) == ("Pack A", "Label A", "house", "oneshot")
    assert "kicks" in k["tags"]
    g = by_path["Loops/Drums/124_G#m_Groove.wav"]
    assert (g["bpm"], g["key"]) == ("124", "G#m")


def test_organize_unmatched_goes_to_unsorted(tmp_path, lib):
    catalog, staging, splice, _ = lib
    (splice / "random").mkdir()
    (splice / "random" / "Mystery?.wav").write_bytes(b"m")
    report = run_organize(catalog, [staging, splice], tmp_path / "drive")
    assert report.unsorted == 1
    assert (tmp_path / "drive" / "Samples" / "Unsorted" / "Mystery_.wav").read_bytes() == b"m"
    text = (tmp_path / "drive" / "Samples" / "catalog.csv").read_text()
    assert "Unsorted/Mystery_.wav" in text


def test_organize_disambiguates_same_basename(tmp_path, lib):
    catalog, staging, splice, _ = lib
    other = make_sample(3, name="Pack C/kicks/Kick 1.wav", pack="Pack C", tags=[("kicks", None)])
    catalog.record_search([other], "", None, 1, 50, ranked=False)
    (splice / "Pack C" / "kicks").mkdir(parents=True)
    (splice / "Pack C" / "kicks" / "Kick 1.wav").write_bytes(b"c" * 99)
    target = tmp_path / "drive"
    report = run_organize(catalog, [staging, splice], target)
    kicks = sorted(p.name for p in (target / "Samples" / "One-Shots" / "Kicks").iterdir())
    assert kicks == ["Kick 1.wav", "Kick 1__Pack C.wav"]
    assert (target / "Samples" / "One-Shots" / "Kicks" / "Kick 1.wav").read_bytes() == b"k" * 10
    assert report.copied == 3
    # the same file seen again in another source is skipped, not duplicated
    dup = tmp_path / "Splice2" / "Kick 1.wav"
    dup.parent.mkdir()
    dup.write_bytes(b"k" * 10)
    report = run_organize(catalog, [staging, splice, dup.parent], target)
    assert report.copied == 0
    assert len(list((target / "Samples" / "One-Shots" / "Kicks").iterdir())) == 2


def test_organize_is_idempotent(tmp_path, lib):
    catalog, staging, splice, _ = lib
    target = tmp_path / "drive"
    run_organize(catalog, [staging, splice], target)
    csv_before = (target / "Samples" / "catalog.csv").read_text()
    report = run_organize(catalog, [staging, splice], target)
    assert report.copied == 0 and report.skipped == 2
    assert (target / "Samples" / "catalog.csv").read_text() == csv_before
    assert not [p for p in target.rglob("*") if p.name.endswith(".partial")]


def test_organize_never_writes_into_sources(tmp_path, lib):
    catalog, staging, splice, _ = lib

    def snapshot():
        return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                      for root in (staging, splice) for p in root.rglob("*"))

    before = snapshot()
    run_organize(catalog, [staging, splice], tmp_path / "drive")
    assert snapshot() == before
    with pytest.raises(ValueError):
        run_organize(catalog, [staging, splice], splice / "lib")


def test_organize_command(tmp_path, cli_env, capsys):
    from splice_curator import cli

    (tmp_path / "staging" / "P").mkdir(parents=True)
    (tmp_path / "staging" / "P" / "x.wav").write_bytes(b"x")
    assert cli.main(["--config", str(cli_env), "organize", str(tmp_path / "drive")]) == 0
    assert "copied 1" in capsys.readouterr().out
    assert (tmp_path / "drive" / "Samples" / "Unsorted" / "x.wav").exists()
