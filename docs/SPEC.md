# better-yt-playlist — implementation plan

## Context

YouTube's playlist UI only offers date/alphabetical sorting. For a large music
playlist, there is no way to answer "show me everything from channel X" or
"everything with 'Y' in the title", and no way to impose a custom order.

The YouTube Data API v3 exposes all of this. Reading is essentially free
(1 quota unit per 50-item page); writing is expensive (50 units per item move
against a 10,000 unit/day budget). So the design is: **mirror the playlist into
local SQLite, query it freely with DuckDB SQL, and push a desired order back to
YouTube slowly over several days.**

The repo (`~/projects/better-yt-playlist`) is bootstrapped with the
dev-standards baseline (uv, Python 3.13, ruff, pre-commit, CI) but has no
source yet.

Clustering by theme is **explicitly out of scope** for now — revisit once
there is real data to look at.

## Verified facts this plan depends on

Checked against Google's docs during planning, not recalled:

- **Quota costs**: `playlistItems.list` = 1, `videos.list` = 1,
  `playlistItems.update` / `insert` / `delete` = 50 each.
  Daily budget is 10,000 units. Worst case (every item must move) a 1000-item
  playlist is ~50,000 units ⇒ ~5–6 days. The LIS-based move selection below
  usually beats this substantially.
- **Refresh tokens expire in 7 days** when the OAuth consent screen is
  external + publishing status "Testing" and any non-trivial scope is
  requested. A multi-day reorder will hit this. Mitigation in Stage 0.

## Architecture decisions

### The reorder engine must not store a move list

`playlistItems.update` with a `position` is a *move* on a live list — every
item between source and destination shifts. A precomputed queue of
"item X → position 42" instructions is invalidated by its own first successful
call.

Instead, the durable artifact is the **target order**: an ordered list of
playlistItem ids. The daily worker recomputes what to move from freshly-fetched
remote state each run.

**Move selection is LIS-based, not a selection sort.** Moves are the entire
cost model (50 units each), so minimizing their count matters directly. The
minimum number of "move item to position p" operations needed to reach a target
permutation is `N − len(longest subsequence already in correct relative target
order)`. A naive left-to-right selection sort can do up to 2× that — e.g.
target `[A,B,C]`, actual `[C,A,B]` needs one move (C→end), but selection sort
does two.

The algorithm per run:

1. Fetch current live order (~20 units).
2. Map each live `playlist_item_id` to its `target_order.rank`.
3. Compute the **longest increasing subsequence** over those ranks in current
   order. Those items are already correctly ordered relative to each other and
   are never touched.
4. Move only the complement, in ascending target-rank order, each to its target
   index — until the day's quota budget is spent.
5. On HTTP 403 `quotaExceeded`, stop cleanly — do not retry.

For a playlist already roughly in the desired order this is a handful of moves
rather than hundreds; for a full 1000-item shuffle it is roughly the difference
between ~6 days and ~3.

This stays idempotent and self-correcting — the LIS is recomputed from fresh
remote state on every run, so resuming is just re-running the command. No
checkpoint file beyond the target order itself.

### Primary key is `playlist_item_id`, never `video_id`

Music playlists contain duplicates — the same video added twice has two
distinct playlistItem ids. Keying on `video_id` would silently corrupt
ordering.

### `playlistItems.update` body shape

Omitted fields are cleared, so the request body must always carry all of:

```python
body = {
    "id": playlist_item_id,
    "snippet": {
        "playlistId": playlist_id,
        "resourceId": {"kind": "youtube#video", "videoId": video_id},
        "position": target_position,
    },
}
```

### Enrichment is a left join; dead entries are tolerated

Long playlists accumulate `Deleted video` / `Private video` entries.
`playlistItems.list` returns them (with no `videoOwnerChannelTitle`);
`videos.list` will not. Enrichment must therefore be a left join, and these
rows still occupy positions during a reorder. Sync **soft-deletes**
(sets `removed_at`) rows missing from a fetch rather than deleting them, so a
transient API blip doesn't destroy local annotations.

## Stages

### Stage 0 — repo hygiene and auth

Critical because secrets land in the working tree immediately.

- **Create `.gitignore` first** — none exists. Must cover `client_secret.json`,
  `token.json`, `*.db`, `.venv/`, `__pycache__/`.
- Add deps to `pyproject.toml`: `google-auth-oauthlib` (for the installed-app
  OAuth flow — `google-api-python-client` alone does not do the consent
  dance), `duckdb`. Commit `uv.lock` — CI's cache key globs it.
- `src/byp/auth.py`: `get_client()` running
  `InstalledAppFlow.run_local_server()` on first use, persisting credentials
  to `token.json`, refreshing silently thereafter. Scope: `youtube` (**not**
  `.readonly` — reordering is a write).
- **Handle the 7-day expiry by re-authing, not by fighting it.** `auth.py`
  catches `google.auth.exceptions.RefreshError`, deletes `token.json`, and
  re-runs the consent flow with a clear message. Expect to re-consent roughly
  weekly during a long reorder. This is a non-event architecturally: the
  reorder is resumable from `target_order` alone, so a dead token on day 7
  costs one browser consent, not lost progress. With LIS-based move selection
  the job will often finish inside 7 days anyway.
- Optional convenience, **not a dependency**: setting the OAuth app's
  publishing status to "In production" *may* remove the 7-day expiry. This is
  unverified — check the Google Cloud console's own wording before relying on
  it, and note that publishing an external app requesting a sensitive scope
  may prompt for verification.

### Stage 1 — sync to SQLite

`src/byp/db.py` — schema via stdlib `sqlite3`:

```sql
CREATE TABLE playlist_items (
    playlist_item_id TEXT PRIMARY KEY,
    playlist_id      TEXT NOT NULL,
    video_id         TEXT NOT NULL,
    position         INTEGER NOT NULL,
    title            TEXT,
    channel_title    TEXT,   -- videoOwnerChannelTitle
    channel_id       TEXT,
    added_at         TEXT,   -- snippet.publishedAt: when added to playlist
    duration_s       INTEGER,-- from videos.list, NULL for dead entries
    description      TEXT,
    tags             TEXT,   -- JSON array
    view_count       INTEGER,
    published_at     TEXT,   -- video's own upload date
    synced_at        TEXT NOT NULL,
    removed_at       TEXT    -- soft delete
);
CREATE TABLE target_order (
    playlist_id      TEXT NOT NULL,
    rank             INTEGER NOT NULL,
    playlist_item_id TEXT NOT NULL,
    PRIMARY KEY (playlist_id, rank)
);
```

`src/byp/sync.py`:
- Page `playlistItems.list` (`maxResults=50`, follow `nextPageToken`).
- Batch the collected `videoId`s 50-at-a-time into `videos.list`
  (`part=contentDetails,snippet,statistics`), left-join onto the items.
- Parse ISO-8601 `PT4M13S` durations to seconds.
- Upsert; stamp `removed_at` on rows not seen this run.
- Report units consumed.

CLI: `byp sync <playlist-id-or-url>`.

### Stage 2 — query

`src/byp/query.py`: open the SQLite file through DuckDB's `sqlite_scanner`
extension and execute arbitrary SQL.

CLI: `byp query "SELECT title, channel_title FROM playlist_items
WHERE channel_title = 'X' AND removed_at IS NULL ORDER BY added_at"`,
plus `--format table|csv|json`.

This is the stage that solves the original complaint, and it costs ~20 quota
units total to get there.

### Stage 3 — reorder

`src/byp/reorder.py`:

- `byp order-from-query "<SQL returning playlist_item_id in desired order>"`
  → writes `target_order`. Validates the result set is a permutation of the
  live items (fail loudly on missing/extra/duplicate ids).
- `byp reorder --budget 9500` → runs the LIS-based move selection above,
  stopping at the budget or on `quotaExceeded`. Prints progress and an ETA in
  days.
- `byp reorder --status` → how many moves remain (`N − LIS`); estimated
  remaining quota and days.
- `--dry-run` prints the move sequence without spending quota.

Quota accounting is tracked in a small `quota_log` table (timestamp, units,
method). The daily quota resets at **midnight Pacific time**, so the budget
check sums units logged **since the last midnight-PT boundary** — not a
rolling 24h window. Every command that spends quota writes to this log,
including `sync`, so that a same-day `sync` plus `reorder --budget 9500`
cannot overrun the 10,000 limit.

### Stage 4 — tests + CI

`ci.yml`'s own header comment says to add a Tests job once a suite exists;
pytest is already in the dev group, so only the job and the tests are missing.

- `tests/test_sync.py` — pagination assembly, left-join with a missing video,
  soft-delete on disappearance, ISO-8601 duration parsing. Mock the API
  client; no network in tests.
- `tests/test_reorder.py` — the important one. Simulate a playlist as a Python
  list where a "move" applies real shift semantics, then assert the algorithm
  converges to the target from arbitrary starting orders, that interrupting it
  partway and resuming still converges, and that **the move count never
  exceeds `N − len(LIS)`**.
- Add the `Tests (Python)` job to `.github/workflows/ci.yml` and register the
  context in the repo's `github-standard.json` required status checks.

## Verification

1. `byp sync <playlist>` on the real playlist; confirm the row count matches
   what YouTube shows and that reported quota use is ~(N/50 * 2) units.
2. `byp query "SELECT DISTINCT channel_title FROM playlist_items"` — spot-check
   a channel against the YouTube UI.
3. Reorder end-to-end on a **throwaway 10-item playlist first**, not the real
   one: set a reversed target order, run `byp reorder`, confirm the live
   playlist matches. Then interrupt mid-run and re-run to confirm resume
   converges.
4. `uv run pytest && uv run ruff check . && uv run ruff format --check .`

## Deliberately out of scope

- Theme clustering (deferred by decision — revisit with real data).
- Creating per-cluster playlists. Note `playlistItems.insert` is also 50
  units, so this is *simpler* than reordering (no shift semantics), **not
  cheaper**.
- Any yt-dlp fallback path.
