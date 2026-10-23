"""Budget groups approved by the owner (spec 7.5).

Credits are fixed by the spec. Queries are a starting point; edit freely.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Group:
    id: str
    title: str
    category: str
    credits: int
    queries: tuple[str, ...]
    want_tags: frozenset[str]


def _g(id, title, category, credits, want, queries) -> Group:
    return Group(id, title, category, credits, tuple(queries), frozenset(want))


GROUPS: tuple[Group, ...] = (
    _g("kicks", "Kicks", "oneshot", 300, {"kicks"},
       ["house kick", "tech house kick", "deep house kick"]),
    _g("snares_claps", "Snares and claps", "oneshot", 300, {"snares", "claps"},
       ["house clap", "house snare", "tech house clap"]),
    _g("hats_cymbals", "Hats, rides, cymbals", "oneshot", 300, {"hats", "cymbals", "rides"},
       ["house hat", "house ride", "open hat house"]),
    _g("perc_oneshots", "Percussion one-shots", "oneshot", 300, {"percussion"},
       ["house percussion", "conga", "bongo", "shaker", "rim"]),
    _g("bass_oneshots", "Bass one-shots", "oneshot", 250, {"bass"},
       ["house bass", "deep house bass", "tech house bass"]),
    _g("synth_stabs", "Synth stabs and chord hits", "oneshot", 150, {"synth", "chords", "stabs"},
       ["house stab", "chord stab", "organ stab"]),
    _g("fx_vocal_oneshots", "FX and vocal one-shots", "oneshot", 150, {"fx", "vocals"},
       ["house vocal shot", "house fx"]),
    _g("drum_loops", "Drum loops (full and tops)", "loop", 450, {"drums", "tops"},
       ["house drum loop", "tech house top loop", "groovy drum loop"]),
    _g("perc_loops", "Percussion loops", "loop", 300, {"percussion"},
       ["house percussion loop", "shaker loop", "conga loop"]),
    _g("bass_loops", "Bass loops", "loop", 250, {"bass"},
       ["house bass loop", "rolling bassline", "tech house bass loop"]),
    _g("chord_loops", "Chord, stab, and keys loops", "loop", 250, {"chords", "keys", "synth", "stabs"},
       ["house chord loop", "organ loop", "deep house keys"]),
    _g("vocal_loops", "Vocal loops", "loop", 160, {"vocals"},
       ["house vocal loop", "disco vocal"]),
)

RESERVE = 200

TARGET_GENRES = frozenset(
    {"house", "tech house", "deep house", "jackin house", "minimal", "disco", "funky house"}
)


def get_group(group_id: str) -> Group:
    for group in GROUPS:
        if group.id == group_id:
            return group
    raise KeyError(f"unknown group {group_id!r}; choose one of: {', '.join(g.id for g in GROUPS)}")
