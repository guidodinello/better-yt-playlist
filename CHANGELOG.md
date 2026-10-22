# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Song similarity via Last.fm: `byp match` maps playlist videos to a canonical
  (artist, track) (`track_metadata` table, with `--set` manual overrides),
  `byp similar` lists playlist songs similar to a seed song, and `byp discover`
  lists similar songs outside the playlist, found on YouTube with yt-dlp (zero
  API quota). When Last.fm has no track-level similarity for a song, both
  fill in from artist similarity (`via artist`). Last.fm results are cached for
  30 days. Needs `BYP_LASTFM_API_KEY`;
  `BYP_SONGS_PLAYLIST` sets the default playlist
- `docs/ROADMAP.md`, including the planned audio-embedding similarity pipeline
- `byp play`: a single YouTube watch link (no API quota) that plays a seed song
  followed by similar playlist songs, optionally mixed with `--new N` songs from
  `discover`, or (`--artist`) the playlist's songs by an artist. `--limit`
  (default 25, max 50), `--shuffle`, `--open`
- `systemd/byp-sync.{service,timer}`: daily `byp sync` + `byp match` of the songs
  playlist, reading a plain `byp.env` (systemd can't parse `.lastfm.env`'s
  `export` lines)
- `PRIVACY.md`, linked from the OAuth consent screen
- `byp serve`: the `play` queues as a phone-sized web page (search, tap a song or
  artist, one Play button). FastAPI + uvicorn in a new optional `serve` extra;
  `systemd/byp-serve.service` runs it on an always-on host
- `byp serve` search matches words in any order, ignores case, accents and
  punctuation, takes prefixes and typos, and ranks the best match first
  (stdlib `difflib`, no new dependency)
- The seed text of `similar`, `discover` and `play` uses the same forgiving
  search; the best match wins, and only a tie between different songs is ambiguous
- `discover --new-artists` and `play --new-artists` skip songs by artists the
  playlist already has (the seed's own included), for songs *and* names you
  don't know yet; `byp serve` has it as an "only new artists" checkbox

- `byp warm`: builds every song's page queue ahead of time (with and without
  "only new artists"), so the page's first tap on a song reads cached results.
  One paced search at a time; a run stops after `--max-minutes` (60) or 10
  failed searches in a row, and the next run continues. `systemd/byp-warm.service`
  runs it after each successful daily sync (`OnSuccess=`)

### Changed
- `byp serve` mixes 5 new songs (from `discover`) into a song's queue by
  default, so discovering doesn't need opting into; set "+ new" to 0 to turn it
  off. `byp play --new` still defaults to 0
- `discover` (and so `play --new` and the page's new songs) runs its yt-dlp
  searches up to 5 at a time, never more than a one-by-one run would make; a
  song's first tap on the page went from ~12 s to ~2–8 s
- When `discover` falls back to artist similarity (Last.fm has no similar
  tracks for the seed), it takes up to 3 top tracks from the seed's own artist
  before one each from similar artists, instead of just 1
- A song with few related songs in the playlist gets new songs filling the gap,
  up to 10 instead of the 5 asked for, so its queue isn't just the seed
  (`play.FILL_NEW_MAX`; "+ new" at 0 still means none). `byp warm` searches as
  many for those songs

### Fixed
- A failed yt-dlp search (error exit or timeout, e.g. YouTube throttling) is no
  longer cached as "not on YouTube" forever; it's skipped and retried later
- `sync` no longer overwrites a video's stored title, channel and metadata with
  YouTube's "Deleted video" / "Private video" placeholder once it becomes
  unavailable; such rows are now flagged with a new `unavailable_at` column
- `import-wl-to-yt` no longer records a "playlist not found" rejection as a
  permanent per-video failure; it stops the run instead, since the error is about
  the target playlist and the video may be fine
- `import-wl-to-yt`'s summary now counts only Watch Later videos as "already"
  imported (it used to include anything else in the target playlist) and reports
  how many are permanently failed
- `import-wl-to-yt` no longer records YouTube's "playlist contains maximum
  number of items" rejection as a permanent per-video failure; it stops the run
  instead, so those videos are retried once the playlist has room
- `import-wl-to-yt` now records a video as permanently failed (`import_failures`
  table) instead of retrying it against the API forever once YouTube rejects it
  outright (dead/private/region-locked)
- Auth: a project needing interactive OAuth re-consent (e.g. after the 7-day
  Testing-app token expiry) is skipped instead of crashing the whole run when
  no browser is available, such as under the systemd timer

### Added
- `byp clean` — delete duplicate copies of the same video (keeping the
  earliest-added one) and deleted/private videos from a playlist, within the
  daily quota (resumes across days); the daily timer runs it before
  `import-wl-to-yt`
- `byp sync` — mirror a YouTube playlist into local SQLite (`playlistItems.list`
  + `videos.list`), with left-join video enrichment and soft-deletion of items
  that disappear between syncs
- `byp query` — run arbitrary DuckDB SQL over the local mirror, `--format
  table|csv|json`
- `byp order-from-query` — store a target playlist order from a SQL result,
  validated as a permutation of the synced items
- `byp reorder` — apply the target order via `playlistItems.update`, bounded by a
  daily quota budget (`--budget`, `--status`, `--dry-run`); LIS-based planner
  keeps moves to a minimum and the run resumes across days
- `byp import-watch-later` — read YouTube Watch Later via yt-dlp (blocked from the
  Data API) into the local DB (`--local`, zero quota) or into a real YouTube
  playlist (`--api`, 50 units per insert; resumable across runs)
- `byp import-wl-to-yt` — daily incremental job that imports remaining Watch Later
  videos into a target playlist, respecting the daily quota, idempotently
- systemd timer/service for automated daily Watch Later import at the PT quota reset
- OAuth 2.0 installed-app flow with automatic re-consent on refresh-token expiry
- Quota accounting table, measured against the midnight-Pacific reset
- Initial project structure with src layout
- GitHub Actions CI (lint, typecheck, security, tests with coverage)
- Pre-commit hooks (ruff, pyupgrade)
- Dependabot for automated dependency updates
- pyright type checking (strict on `src/`, standard on tests)
- pip-audit for vulnerability scanning

## [0.1.0] - 2026-08-28

### Added
- Project bootstrap with dev-standards baseline
- Basic CLI entry point
- Initial test suite
