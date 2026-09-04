# better-yt-playlist

Manage a large YouTube playlist the way its web UI won't let you: mirror it
into local SQLite, slice it with arbitrary SQL, and push a custom order back to
YouTube a little at a time.

## Why

The YouTube playlist UI only sorts by date-added or title. It can't answer
"everything from channel X", "everything with 'Y' in the title", or "put these
in *this* order". The Data API can do all of it — reads are almost free
(1 quota unit per 50 items), writes are expensive (50 units per move, against a
10,000/day budget), so ordering is done gradually over several days.

## Setup

1. In the [Google Cloud console](https://console.cloud.google.com/): create a
   project, enable **YouTube Data API v3**, configure an OAuth consent screen
   (External), and create an **OAuth Client ID** of type **Desktop app**.
2. Download the client JSON to `./client_secret.json` (git-ignored).
3. `uv sync --dev`

The first command that touches the API opens a browser for consent and writes
`./token.json`. A refresh token from a "Testing" consent screen expires after
7 days; when that happens the tool just re-runs consent — reorder progress is
stored in the database, not the token, so nothing is lost.

Paths are overridable: `BYP_DB`, `BYP_CLIENT_SECRET`, `BYP_TOKEN`.

## Usage

```bash
# 1. Mirror a playlist (accepts a bare id or any URL with list=)
byp sync "https://www.youtube.com/playlist?list=PLxxxxxxxx"

# 2. Import Watch Later (blocked from the YouTube API — reads via yt-dlp)
byp import-watch-later --browser firefox          # local DB, zero quota
byp import-watch-later --browser firefox --api    # create real YouTube playlist

# 3. Query it — full DuckDB SQL over the `playlist_items` table
byp query "SELECT title, channel_title, duration_s
           FROM playlist_items
           WHERE channel_title = 'Some Channel' AND removed_at IS NULL
           ORDER BY added_at"
byp query "SELECT count(*) FROM playlist_items WHERE title ILIKE '%live%'" --format csv

# 4. Define a target order — SQL returning playlist_item_id, in the order you want
byp order-from-query "SELECT playlist_item_id FROM playlist_items
                      WHERE removed_at IS NULL ORDER BY channel_title, title"

# 5. Apply it, respecting the daily quota (re-run daily until done)
byp reorder --status        # how many moves / days remain
byp reorder --dry-run       # list the moves without spending quota
byp reorder                 # apply up to --budget units today (default 9500)
```

### Columns in `playlist_items`

`playlist_item_id` (PK), `video_id`, `position`, `title`, `channel_title`,
`channel_id`, `added_at`, `duration_s`, `description`, `tags` (JSON), `view_count`,
`published_at`, `synced_at`, `removed_at`.

`channel_title` / `duration_s` etc. are `NULL` for deleted or private videos —
those rows stay in the mirror (and keep their playlist slot). A row that
disappears from the playlist between syncs gets a `removed_at` timestamp rather
than being deleted.

## How reorder works

`order-from-query` stores the desired sequence of `playlist_item_id`s. Each
`reorder` run fetches the current live order, computes the minimum set of moves
to reach the target (keeping the longest already-correct subsequence fixed), and
applies them until the daily quota budget is hit or YouTube reports the quota
exhausted. Re-running the next day picks up where it left off. Run `byp sync`
again afterward to refresh local `position` values.

## Watch Later

YouTube's Watch Later playlist is blocked from the Data API (since 2016) — it
cannot be reordered or written to programmatically. This tool reads it via
yt-dlp using browser cookies and stores it in the local DB. Mirroring it into a
real playlist (API mode, below) is what lets `byp sync`/`query`/`reorder` be
used against Watch Later's contents at all, since those commands need a
playlist the API can write to.

**Local mode** (default) — zero quota, writes straight to SQLite:
```bash
byp import-watch-later --browser firefox
```

**API mode** — creates a real YouTube playlist and inserts videos via the API
(50 quota units per insert, ~199/day max):
```bash
byp import-watch-later --browser firefox --api --name "My Watch Later"
```

### Automating the API import

A systemd timer runs daily at 00:05 PT (after quota resets) and imports the
next batch of remaining videos:

```bash
# Check timer status
systemctl --user status byp-import-wl.timer

# View logs
journalctl --user -u byp-import-wl.service

# Disable and clean up
systemctl --user disable --now byp-import-wl.timer
rm ~/.config/systemd/user/byp-import-wl.{service,timer}
```

At ~199 videos/day this takes a while for a large backlog — that's expected.
A YouTube Data API quota increase was considered and rejected: the request
form routes through a compliance audit meant for public-facing apps with real
users (privacy policy, ToS, expected user base), not a personal single-user
script, so approval odds are low. Using a second GCP project/key to double the
effective quota was also considered and rejected — it's an explicit violation
of the YouTube API Services Terms and risks suspension. Since the local
zero-quota import already captures the full backlog immediately, the API
mirror's slower pace isn't blocking anything — it only affects when the real
playlist is ready for `sync`/`reorder`.

Timer and service files live in `systemd/` and are symlinked to
`~/.config/systemd/user/`.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
pre-commit install
```

## Not (yet) included

Theme/genre clustering, and creating per-cluster playlists.
