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
    subtypes: tuple[str, ...] = ()


def _g(id, title, category, credits, want, queries, subtypes=()) -> Group:
    return Group(id, title, category, credits, tuple(queries), frozenset(want), tuple(subtypes))


GROUPS: tuple[Group, ...] = (
    _g("kicks", "Kicks", "oneshot", 300, {"kicks"},
       ["house kick", "tech house kick", "deep house kick"],
       ("kick",)),
    _g("snares_claps", "Snares and claps", "oneshot", 300, {"snares", "claps"},
       ["house clap", "house snare", "tech house clap"],
       ("clap", "snare")),
    _g("hats_cymbals", "Hats, rides, cymbals", "oneshot", 300, {"hats", "cymbals", "rides"},
       ["house hat", "house ride", "open hat house"],
       ("closed hat", "open hat", "ride")),
    _g("perc_oneshots", "Percussion one-shots", "oneshot", 300, {"percussion"},
       ["house percussion", "conga", "bongo", "shaker", "rim"],
       ("conga", "bongo", "shaker", "rim", "tom")),
    _g("bass_oneshots", "Bass one-shots", "oneshot", 250, {"bass"},
       ["house bass", "deep house bass", "tech house bass"],
       ("bass one shot", "sub bass", "bass stab")),
    _g("synth_stabs", "Synth stabs and chord hits", "oneshot", 150, {"synth", "chords", "stabs"},
       ["house stab", "chord stab", "organ stab"],
       ("chord stab", "organ stab", "keys")),
    _g("fx_vocal_oneshots", "FX and vocal one-shots", "oneshot", 150, {"fx", "vocals"},
       ["house vocal shot", "house fx"],
       ("vocal chop", "vocal shout", "riser", "impact")),
    _g("drum_loops", "Drum loops (full and tops)", "loop", 450, {"drums", "tops"},
       ["house drum loop", "tech house top loop", "groovy drum loop"],
       ("drum loop", "top loop")),
    _g("perc_loops", "Percussion loops", "loop", 300, {"percussion"},
       ["house percussion loop", "shaker loop", "conga loop"],
       ("percussion loop", "shaker loop", "conga loop")),
    _g("bass_loops", "Bass loops", "loop", 250, {"bass"},
       ["house bass loop", "rolling bassline", "tech house bass loop"],
       ("bass loop", "bassline")),
    _g("chord_loops", "Chord, stab, and keys loops", "loop", 250, {"chords", "keys", "synth", "stabs"},
       ["house chord loop", "organ loop", "deep house keys"],
       ("chord loop", "organ loop", "keys loop")),
    _g("vocal_loops", "Vocal loops", "loop", 160, {"vocals"},
       ["house vocal loop", "disco vocal"],
       ("vocal loop", "vocal phrase")),
)

RESERVE = 200


@dataclass(frozen=True)
class Genre:
    id: str
    title: str
    weight: float
    tags: frozenset[str]
    prefixes: tuple[str, ...]
    bpm: tuple[int, int]


def _genre(id, title, weight, tags, prefixes, bpm) -> Genre:
    return Genre(id, title, weight, frozenset(tags), tuple(prefixes), tuple(bpm))


GENRES: tuple[Genre, ...] = (
    _genre("tech_house", "Tech house", 0.40, {"tech house"}, ["tech house"], (124, 128)),
    _genre("minimal", "Minimal / groovy tech house", 0.25,
           {"minimal techno", "minimal", "tech house"},
           ["minimal house", "groovy tech house"], (124, 128)),
    _genre("deep_house", "Deep house", 0.15, {"deep house"}, ["deep house"], (118, 124)),
    _genre("afro_house", "Afro house / afro tech", 0.10, {"afro house", "afro tech"},
           ["afro house"], (118, 124)),
    _genre("classic_house", "Classic / jackin house", 0.10, {"house", "disco", "nu disco"},
           ["jackin house", "disco house"], (120, 126)),
)

TARGET_GENRES = frozenset(
    {"house", "tech house", "deep house", "jackin house", "minimal", "disco", "funky house"}
)


def get_group(group_id: str) -> Group:
    for group in GROUPS:
        if group.id == group_id:
            return group
    raise KeyError(f"unknown group {group_id!r}; choose one of: {', '.join(g.id for g in GROUPS)}")


def get_genre(genre_id: str, genres: tuple[Genre, ...] = GENRES) -> Genre:
    for genre in genres:
        if genre.id == genre_id:
            return genre
    raise KeyError(f"unknown genre {genre_id!r}; choose one of: {', '.join(g.id for g in genres)}")


def _split(total: int, weights: list[float]) -> list[int]:
    """Largest-remainder split of ``total``; earlier items win ties."""
    raw = [total * w for w in weights]
    parts = [int(r + 1e-9) for r in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - parts[i]), i))
    for i in order[: total - sum(parts)]:
        parts[i] += 1
    return parts


def genre_quotas(group: Group, genres: tuple[Genre, ...] = GENRES, total: int | None = None) -> dict[str, int]:
    """Split a group's credits (or ``total``) across genres by weight."""
    credits = group.credits if total is None else total
    return dict(zip((g.id for g in genres), _split(credits, [g.weight for g in genres])))


def subtype_quotas(group: Group, total: int) -> dict[str, int]:
    """Split ``total`` evenly across the group's subtypes."""
    n = len(group.subtypes)
    return dict(zip(group.subtypes, _split(total, [1 / n] * n))) if n else {}
