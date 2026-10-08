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

### Songs: similar and discover (Last.fm)

For a music playlist, `byp` can find songs *like* one you already have, using
[Last.fm](https://www.last.fm)'s similarity data. You need a free API key
(<https://www.last.fm/api/account/create>); no Last.fm account login is
involved, and none of this spends YouTube API quota.

```bash
export BYP_LASTFM_API_KEY=...            # required
export BYP_SONGS_PLAYLIST=PLxxxxxxxx     # default --playlist for the commands below

byp sync "$BYP_SONGS_PLAYLIST"           # mirror it first
byp match                                # map each video to a Last.fm (artist, track)
byp match --retry                        # also retry ones not found last time
byp match --set VIDEO_ID "Artist" "Track"   # fix one by hand; never overwritten

byp similar "loser tame impala"          # songs in the playlist similar to that one
byp discover "loser tame impala"         # similar songs NOT in it, with YouTube links
byp discover vIdEoId --limit 5 --format json
```

The seed is a video id or any text in the song's title/artist/track; if it
matches several songs you get the list to pick from. Many less-known songs
have no track-level similarity on Last.fm; then (and whenever track-level
results come up short) both commands fill in from **similar artists** —
marked `via artist` in the output. That fallback is unreliable for artist
names shared by several acts (Last.fm merges them into one profile). `discover` finds each
recommendation on YouTube with a yt-dlp search (no quota). Similarity results
are cached for 30 days and YouTube lookups indefinitely, so repeat runs are
instant.

#### Play: a watch queue from a song or an artist

`byp play` turns those results into one YouTube link that plays them in order
(`watch_videos?video_ids=…`, which YouTube opens as a temporary, unsaved
playlist). No login and no API quota.

```bash
byp play "loser tame impala"             # the song, then similar playlist songs
byp play "loser" --new 5                 # mix in 5 similar songs you don't have yet
byp play --artist "Tame Impala"          # the playlist's songs by an artist
byp play "loser" --limit 40 --shuffle --open   # longer, shuffled, opened in the browser
```

`--shuffle` keeps the seed first and shuffles the best matches after it; with
`--artist` it picks from all of the artist's songs.

The queue is 25 songs by default and at most 50: the endpoint silently drops
every id past the 50th. It's undocumented, so each song's own link is printed
too. `--new` songs come from `discover` (a yt-dlp search per song the first
time, so slower). `--artist` matches the full credit or its first-listed name
— a song credited "Dua Lipa, Tame Impala" is found by "Dua Lipa" only.

To just list an artist's songs, query the mirror once songs are matched:

```bash
byp query "SELECT p.title FROM playlist_items p JOIN track_metadata m USING (video_id)
           WHERE m.artist = 'Tame Impala' AND p.removed_at IS NULL"
```

Similarity data: Last.fm. See [`docs/ROADMAP.md`](docs/ROADMAP.md) for what's
next (audio-based similarity).

### Columns in `playlist_items`

`playlist_item_id` (PK), `video_id`, `position`, `title`, `channel_title`,
`channel_id`, `added_at`, `duration_s`, `description`, `tags` (JSON), `view_count`,
`published_at`, `synced_at`, `removed_at`, `unavailable_at`.

Deleted or private videos have `unavailable_at` set (when a sync first saw them
dead) — those rows stay in the mirror (and keep their playlist slot). They keep
whatever title/channel/duration an earlier sync recorded; one first seen
already dead only has YouTube's `Deleted video` / `Private video` placeholder. A row that
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
(50 quota units per insert, ~199/day max per project):
```bash
byp import-watch-later --browser firefox --api --name "My Watch Later"
```

### Automating the API import

A systemd timer runs daily at 00:05 PT (after quota resets) and imports the
next batch of remaining videos into the target playlist via `byp import-wl-to-yt`:

```bash
# Check timer status
systemctl --user status byp-import-wl.timer

# View logs
journalctl --user -u byp-import-wl.service

# Disable and clean up
systemctl --user disable --now byp-import-wl.timer
rm ~/.config/systemd/user/byp-import-wl.{service,timer}
```

Before importing, the service runs `byp clean`, which deletes two kinds of
entry from the target playlist: extra copies of a video that is in it more than
once (the API allows duplicates, and an early version of the import created ~600
of them; the earliest-added copy is kept), and videos that have since been
deleted or made private (whatever `videos.list` no longer returns). Each
deletion costs 50 units; once the playlist is clean a run only costs the
listing plus the video lookup (~200 units). YouTube caps a playlist at 5,000
items, and a "playlist full" rejection stops the import without marking any
video as failed.

Timer and service files live in `systemd/` and are symlinked to
`~/.config/systemd/user/`. The service calls the installed `byp` entry point and
reads the target playlist from `BYP_TARGET_PLAYLIST`.

### Spreading the one-off backlog across two projects

A YouTube Data API quota increase was considered and rejected: the request
form routes through a compliance audit meant for public-facing apps with real
users (privacy policy, ToS, expected user base), not a personal single-user
script, so approval odds are low.

Using a second GCP project/OAuth client to add a second independent 10,000
unit/day budget was also considered risky — it's against the letter of the
YouTube API Services Terms, which prohibit working around one app's quota with
multiple projects, and could in principle get both projects suspended. It was
used anyway for this one-off backlog import, accepting that risk consciously
rather than as a default pattern: `import-wl-to-yt` spends against `PROJECTS =
["default", "2"]` in `import_wl_to_yt.py`, falling through to the second
project once the first's daily quota is exhausted. Each project needs its own
`client_secret_<name>.json` / `token_<name>.json` (see `auth.py`'s
`_paths_for`) and its own OAuth consent screen with your account added as a
test user — both `better-yt-playlist` (default) and `better-yt-playlist-2`'s
test-user allowlists are `guido.dinello@gmail.com`; picking a different
signed-in Google account (e.g. guidoobolso@gmail.com) in the consent screen's
account chooser fails with a 403 `access_denied` even though the tool itself
otherwise works fine. Once the backlog is caught up, drop back to a single
project.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
pre-commit install
```

## Not (yet) included

Theme/genre clustering, and creating per-cluster playlists.
