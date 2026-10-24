# Splice API notes

Captured 2026-10-23 from splice.com in a logged-in Chrome session. Secret
values are not recorded here.

## Request shape the site sends

The search page (`/sounds/search/samples`) renders the first page on the
server. A client request is sent when a filter changes (for example a tag
pill).

- `POST https://surfaces-graphql.splice.com/graphql`
- Sent with `fetch(..., { credentials: "include" })`, so the browser also
  sends the `splice.com` cookies, plus its own `Origin: https://splice.com`,
  `Referer` (the current splice.com page), and `User-Agent`.
- Headers set by the site's code (names only; values of the first and
  third could not be read because of a tool filter, standard values
  assumed):
  - `accept` — assume `application/json`
  - `authorization` — `Bearer <_splice_token_prod cookie value>`
  - `content-type` — assume `application/json`
  - `x-anonymous-id` — analytics anonymous ID. The tool generates one
    random UUID per install and stores it in the config file.
- Chrome User-Agent captured on macOS (default for `Config.user_agent`):
  `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36`
- Cookies known by name: `_splice_token_prod` (session JWT, readable from
  JavaScript), `XSRF-TOKEN`. Send both when available. The cookie set sent
  with env-var auth is `_splice_token_prod` only.
- `operationName`: `SamplesSearch`. Variable keys the site sends:
  `order, sort, limit, page, tags, key, chord_type, bpm, min_bpm, max_bpm,
  asset_category_slug, random_seed, attributes, filepath, query, ac_uuid`.
  The site no longer sends `legacy`; queries with
  `filter.legacy: true` and `legacy: {use: true}` still work (verified).

## Value formats (verified with read-only queries)

- `key`: lowercase note name, sharps with `#`: `g#`, `c`, `a`, `g`. `null`
  when not set. One-shots usually have `null` BPM and key.
- `chord_type`: `minor` or `major` (only `minor` seen in this sample);
  `null` when not set.
- `bpm`: integer, for example `124`, `128`.
- `duration`: milliseconds (for example `7500`).
- Search results `files[].asset_file_type_slug`: `preview_mp3`, `waveform`
  only. No source file in search results.

## Asset lookup for an owned sample (verified, read-only)

`asset(uuid: <catalog_uuid>) { __typename ... on SampleAsset { licensed files(includeSource: true) { asset_file_type_slug url } } }`

- `__typename: SampleAsset`, `licensed: true`.
- Files: `preview_mp3` (S3, `.mp3`), `waveform` (`spliceblob.splice.com`,
  `.json`), `source` (host `spliceproduction.s3.us-west-1.amazonaws.com`,
  path `/audio_samples/<hash>` with **no file extension**, signed query
  string). Take the file name from the sample `name`, not from the URL.

## Autobuy queries (verified live 2026-10-24, read-only)

- `asset(uuid) { ... on SampleAsset { asset_prices { amount currency } } }`:
  a one-shot sample returns `amount 1`, `currency "Credits"` (both owned and
  not owned). The tool buys only when the parsed price equals the planned one.
- Pack search needs `filter: {legacy: true, published: true, asset_type_slug:
  pack, query}` and `legacy: {use: true}`. Without them the pack `uuid`s are
  a different kind and `children` returns 404.
- Pack ids are legacy ids: hex with odd groups, for example
  `796e02ba-0b2e-8680-7860-b3387cec28998dfb579`. They are not standard UUIDs.
- In-pack sounds: `assetsSearch(filter: {legacy: true, published: true,
  asset_type_slug: sample, asset_category_slug: oneshot|loop}, children:
  {parent_asset_uuid: "<pack id>"}, ...)`. `children` is a sibling of
  `filter`, and `AssetChildrenInput` has no `asset_type_slug`.
- `SamplesSearch` parents now also return the pack `uuid` (legacy id).
- `legacyPacksChart(searchParams: {perPage: 100})` works (a list); per-label
  charts use `providerPermalink`. `wwwPremiumProviders(sort: "chart",
  per_page: 50)` returns a list. The global charts hold non-house packs
  (hip hop, trap), so the tool filters labels by `tags_array`.

## Not verified

- `purchaseAsset(uuid)` mutation (from spliceclaw). First verified in plan
  Task 12 with 1 credit.
- Rate limits. None published.
