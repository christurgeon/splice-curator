# splice-curator

A command-line tool to search the Splice sample catalog with **your own**
logged-in session, build shortlists you review by ear, license only the
sounds you approve, download every sound you own, and build an organized,
cross-platform (exFAT-safe) copy of your library.

## Disclaimer

- **Unofficial.** This project is not affiliated with, endorsed by, or
  supported by Splice.
- It uses **your own Splice account** and your own browser session token.
- It **spends your credits only with explicit confirmation**: `buy` is a dry
  run unless you pass `--confirm`, and a purchase is never retried
  automatically.
- It **does not bypass licensing**. It only downloads sounds you already
  own or have just licensed with your credits. Preview audio is streamed in
  the review page for listening only and is never saved to disk.
- Splice publishes no API. **Automated access may break Splice's terms of
  service and put your account at risk.** Requests are paced slowly on
  purpose; use at your own risk.

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync                 # core
uv sync --extra chrome  # optional: read the token from Chrome's cookie store
```

## Authentication

Either export the token (the value of the `_splice_token_prod` cookie on
splice.com) as `SPLICE_TOKEN`, or pass `--source chrome` to read it from
your local Chrome profile. The token is never printed, logged, or written
to disk. Runs stop with a clear message when it has expired.

## Configuration

`~/.splice-curator/config.toml` (created on first run). Every value has a
default; per-run overrides are available as flags.

```toml
staging_dir = "/Volumes/SSD/SpliceCurator/downloads"
max_purchases_per_day = 400
per_pack_cap = 5
refresh_margin_s = 300   # renew the login this long before it expires
refresh_poll_s = 30      # seconds between Chrome checks while waiting
refresh_wait_s = 900     # give up waiting for Chrome after this long

[delays.search]
min_s = 3
max_s = 6

[delays.purchase]
min_s = 6
max_s = 12
```

Delay kinds: `search`, `account`, `asset`, `purchase`, `download`. All
requests share one throttle; there are never parallel requests to Splice.

## Commands

| Command | Spends credits | What it does |
|---|---|---|
| `auth` | no | Show token expiry, username, and credits. |
| `search` | no | Search one budget group over N pages and store results in the local catalog. |
| `shortlist` | no | Rank catalog rows for one group (and genre) and write an HTML review page. |
| `quota` | no | Show each genre's quota, searched, shortlisted, bought, and remaining counts for a group. |
| `buy` | **yes, with `--confirm`** | License the sounds in an approved list. |
| `sync` | no | Download every owned sound that is not on disk yet. |
| `organize` | no | Copy downloaded files into an organized library. |

```bash
uv run splice-curator auth [--source env|chrome]
uv run splice-curator search --group kicks [--genre minimal] [--pages 2] [--query "house kick"]
uv run splice-curator shortlist --group kicks --genre tech_house [--size 120]
uv run splice-curator quota --group kicks
uv run splice-curator buy approved-<batch>.json                 # dry run
uv run splice-curator buy approved-<batch>.json --confirm --max-credits 50
uv run splice-curator sync [--limit N] [--no-refresh]
uv run splice-curator organize /Volumes/SSD/Library
```

Global flags (before the command): `--config PATH`, `--source env|chrome`,
`--max-per-day N`, `--delay KIND=MIN:MAX` (repeatable).

### Genres and quotas

Each budget group's credits are split across genres by weight, and each
genre's share is spread evenly across the group's sound sub-types (for
example kick, clap, snare). `search` queries `"<prefix> <subtype>"` for each
genre prefix and sub-type. `shortlist --genre` picks sounds whose tags (or
pack genre) match the genre, with these limits: per-pack and per-label caps,
no near-duplicate names within a pack, a BPM window for loops, and at most
15% of a bass or chord loop shortlist in one key and chord type. The default
`--size` is the genre's remaining quota (sounds already shortlisted or owned
count as spent), capped at `batch_size`. Batch ids look like
`kicks-tech_house-20260101-120000`; review one batch per group and genre.
`--query` still runs one free-text query with no genre.

| Id | Title | Weight | Loop BPM |
|---|---|---|---|
| `tech_house` | Tech house | 0.40 | 124-128 |
| `minimal` | Minimal / groovy tech house | 0.25 | 124-128 |
| `deep_house` | Deep house | 0.15 | 118-124 |
| `afro_house` | Afro house / afro tech | 0.10 | 118-124 |
| `classic_house` | Classic / jackin house | 0.10 | 120-126 |

Change genres in the config file with a `[genres.<id>]` table. For a known
id, missing keys keep the default. A new id needs `title`, `weight`, `tags`,
`prefixes`, and `bpm`. `weight = 0` disables a genre. Enabled weights must
sum to 1.0. This example disables afro house and gives its share to tech house:

```toml
[genres.afro_house]
weight = 0

[genres.tech_house]
weight = 0.50
```

### Review flow

1. `shortlist` writes `review/<batch_id>.html` under the data folder. Open
   it in a browser.
2. Keys: `j`/`k` next/previous row, `space` play/pause, `x` toggle the row.
   "Toggle pack" approves or rejects a whole pack.
3. Click **Export approved** to save `approved-<batch_id>.json`.
4. Run `buy` with that file: first without `--confirm` to see the plan.

### Organized layout

```
Samples/
  One-Shots/  Kicks/ Snares & Claps/ Hats & Cymbals/ Percussion/ Bass/ Synth/ FX/ Vocals/ Other/
  Loops/      Drums/ Percussion/ Bass/ Chords & Keys/ Vocals/ Other/
  Unsorted/
  catalog.csv
```

Files are copied (never moved or linked); `~/Splice` is never modified.
File names are `<BPM>_<Key>_<original name>`, with missing parts omitted.

## Development

```bash
git config core.hooksPath .githooks   # commit guard: no audio, DBs, JWTs, or username
uv run pytest -v
```

The commit guard reads the owner's username from `.local/username`
(gitignored) and rejects commits that contain it.

## Credits

The purchase and source-download flow follows
[eiei114/spliceclaw](https://github.com/eiei114/spliceclaw) (MIT/Apache-2.0).
Field names for search also draw on
[ascpixi/splicedd](https://github.com/ascpixi/splicedd).

## License

MIT
