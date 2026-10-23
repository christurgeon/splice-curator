from pathlib import Path

from check_commit import check_paths


def _fake_jwt() -> str:
    # Built at runtime so no JWT-shaped literal is ever committed.
    return "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12


def test_rejects_audio_and_db(tmp_path):
    paths = []
    for name in ("a.wav", "b.sqlite", "c.asd"):
        p = tmp_path / name
        p.write_bytes(b"\x00\x01")
        paths.append(p)
    assert len(check_paths(paths, None)) == 3


def test_rejects_jwt_in_text(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text(f"token: {_fake_jwt()}\n")
    problems = check_paths([p], None)
    assert len(problems) == 1
    assert _fake_jwt() not in problems[0]


def test_rejects_username(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("path users/default/someuser/sounds.db\n")
    assert len(check_paths([p], "someuser")) == 1


def test_clean_files_pass(tmp_path):
    p = tmp_path / "README.md"
    p.write_text("# Hello\n\nNormal text about house kicks.\n")
    assert check_paths([p], "someuser") == []


def test_missing_paths_are_ignored(tmp_path):
    assert check_paths([tmp_path / "gone.txt"], None) == []


def test_username_match_is_case_insensitive(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("SomeUser was here")
    assert len(check_paths([p], "someuser")) == 1
