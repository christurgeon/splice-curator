from splice_curator.paths import safe_part


def test_replaces_forbidden_chars():
    assert safe_part("a:b?.wav") == "a_b_.wav"
    assert safe_part('<>:"/\\|?*.wav') == "_________.wav"
    assert safe_part("tab\there\x7f.wav") == "tab_here_.wav"


def test_truncates_keeping_extension():
    name = "x" * 300 + ".wav"
    out = safe_part(name)
    assert len(out.encode()) <= 255
    assert out.endswith(".wav")
    multibyte = "é" * 200 + ".wav"
    out = safe_part(multibyte)
    assert len(out.encode()) <= 255 and out.endswith(".wav")
    out.encode().decode()  # still valid UTF-8


def test_strips_trailing_dots_and_spaces():
    assert safe_part("name. ") == "name"
    assert safe_part("...") == "_"
    assert safe_part("") == "_"


def test_reserved_windows_names():
    assert safe_part("CON") == "_CON"
    assert safe_part("nul.wav") == "_nul.wav"
    assert safe_part("console.wav") == "console.wav"


def test_custom_max_bytes():
    assert len(safe_part("abcdefghij.wav", max_bytes=8).encode()) <= 8
    assert safe_part("abcdefghij.wav", max_bytes=8).endswith(".wav")
