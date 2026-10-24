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

With `--source chrome`, long runs pick up the renewed token from Chrome.
Splice renews it only when a splice.com page loads after the old token has
expired, so on macOS the tool then runs
`open -g -a "Google Chrome" https://splice.com/sounds` once per expired token
(Chrome stays in the background), logs one line, and keeps checking Chrome's
cookies until `refresh_wait_s` ends. Chrome must be running and logged in.

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
| `discover` | no | Find the best packs per genre (charts, popularity, recent) and their best sounds. |
| `autobuy plan` | no | Choose sounds for the live balance minus `keep`; saves only with `--confirm`. |
| `autobuy run` | **yes, with `--confirm`** | Buy the sounds of a saved plan, across several days if needed. |
| `release` | no | Return unbought planned sounds to the quotas. |
| `sync` | no | Download every owned sound that is not on disk yet. |
| `organize` | no | Copy downloaded files into an organized library. |

```bash
uv run splice-curator auth [--source env|chrome]
uv run splice-curator search --group kicks [--genre minimal] [--pages 2] [--query "house kick"]
uv run splice-curator shortlist --group kicks --genre tech_house [--size 120]
uv run splice-curator quota --group kicks
uv run splice-curator buy approved-<batch>.json                 # dry run
uv run splice-curator buy approved-<batch>.json --confirm --max-credits 50
uv run splice-curator discover [--genre tech_house] [--force]
uv run splice-curator autobuy plan [--group all|kicks] [--replace] [--page] [--confirm]
uv run splice-curator autobuy run plan-<id>.json                      # dry run
uv run splice-curator autobuy run plan-<id>.json --confirm --until-done
uv run splice-curator release [BATCH] [--rejected rejected-<id>.json]
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

### Autobuy

Autobuy chooses sounds by popularity signals instead of by ear, then buys
them within the daily cap. Presets are not covered.

1. `discover` (free, read-only) reads the weekly pack and label charts, the
   most popular packs per enabled genre (all time and recent), and the most
   popular one-shots and loops inside each kept pack. Results are cached
   (`discovery_ttl_days`, `chart_ttl_days`); a full first run takes about 40
   minutes at the default pacing.
2. `autobuy plan` reads the live balance, subtracts `keep`, and scores every
   candidate (see `[scoring]`). It prints capacity per group and genre,
   credits by group and genre, the share from charted and recent packs, top
   packs and labels, score percentiles, storage need, and the buying days
   needed against `deadline`. **Without `--confirm` nothing is saved.** With
   `--confirm` it marks batches (ids end in `-ab`), writes
   `approved-<batch>.json`, `plans/plan-<id>.json`, and `plans/plan-<id>.csv`
   (pack, label, score, why, splice.com link). `--page` also writes
   `review/plan-<id>.html`: untick sounds you dislike and click "Export
   rejected list". Saving a plan never spends credits.
3. `autobuy run PLAN.json` is a dry run unless `--confirm`. It buys in plan
   order (groups interleaved, so an early stop leaves a balanced library) and
   skips `rejected-<id>.json` if it sits next to the plan. It re-reads each
   sound before buying it and skips sounds that left their batch while it ran
   (`release`, `plan --replace`). It stops when the balance would fall below
   `keep`. `--until-done` sleeps until midnight at the daily cap, checks the
   balance against the logged spend, and continues. Run it under
   `caffeinate -i` with a splice.com tab open in Chrome:

   ```bash
   caffeinate -i uv run splice-curator --source chrome autobuy run plan-<id>.json --confirm --until-done
   ```

4. `release` clears the batch mark of unbought sounds of all autobuy plans
   (or one batch id) so planning can start again; `--replace` on `plan` does
   the same first. `release --rejected FILE` removes rejected sounds from the
   quotas for good.

Spending guards (also used by `buy`): a sound is bought only if Splice shows
a price in credits equal to the planned price (else it is skipped and logged
as `price_unknown` or `price_mismatch`; the run stops after
`max_price_skips`); budgets and `--max-credits` count credits; every 25
purchases the balance is compared with the logged spend and a mismatch stops
the run; a lock file (`buy.lock`) allows one buying process at a time; sounds
already owned in the catalog cost no request; each purchase is logged as
`uncertain` before it is sent and settled after, so an `uncertain` row (also
one left by a crash or Ctrl-C) is checked read-only before the next run; a
purchase counts toward the daily cap of the day it finished; a sound whose
license check fails is skipped and logged as `check_failed` (it counts toward
`max_price_skips`); staging free space is checked before each buy.

The plan never places more than live balance minus `keep` minus credits
already pending in unbought batches (manual ones too).

New config keys (all optional):

```toml
keep = 200                  # credits left after the plan
deadline = 2026-11-06       # your next charge date
max_price_skips = 5
recent_months = 30          # what counts as a recent pack
packs_per_genre = 100
pack_pages = 8              # pages of 50 packs read per genre prefix
discovery_ttl_days = 7
chart_ttl_days = 7
chart_labels = 15           # labels whose own weekly pack chart is read
max_per_pack_per_group = 6
max_per_pack_total = 25
per_label_share = 0.15      # of a group's target
max_label_share_total = 0.12  # of the whole plan; unplaced credits are reported
key_share_cap = 0.15        # bass and chord loops
floor_high = 40             # score percentile floors
floor_low = 10
prefer_packs = []           # +0.3 score
prefer_labels = []
exclude_packs = []          # never picked
exclude_labels = []
exclude_pack_patterns = ["KSHMR"]  # case-insensitive pack name substrings; not discovered or picked

[scoring]                   # weights; defaults shown in src/splice_curator/config.py
in_pack_pop = 1.0
query_pop = 0.6
pack_pop = 0.4
pack_recent_pop = 0.5
trending = 0.4
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
