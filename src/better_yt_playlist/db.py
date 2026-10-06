"""SQLite storage: schema, connection helper, and quota accounting.

The database is a local mirror of a YouTube playlist plus a desired target
order and a log of API quota spent. Primary key is ``playlist_item_id`` (not
``video_id``) because a playlist may contain the same video more than once.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# YouTube Data API daily quota resets at midnight US Pacific time.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")
DAILY_QUOTA = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_item_id TEXT PRIMARY KEY,
    playlist_id      TEXT NOT NULL,
    video_id         TEXT NOT NULL,
    position         INTEGER NOT NULL,
    title            TEXT,
    channel_title    TEXT,
    channel_id       TEXT,
    added_at         TEXT,
    duration_s       INTEGER,
    description      TEXT,
    tags             TEXT,
    view_count       INTEGER,
    published_at     TEXT,
    synced_at        TEXT NOT NULL,
    removed_at       TEXT,
    unavailable_at   TEXT
);

CREATE TABLE IF NOT EXISTS target_order (
    playlist_id      TEXT NOT NULL,
    rank             INTEGER NOT NULL,
    playlist_item_id TEXT NOT NULL,
    PRIMARY KEY (playlist_id, rank)
);

CREATE TABLE IF NOT EXISTS quota_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    units   INTEGER NOT NULL,
    method  TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS import_state (
    key      TEXT PRIMARY KEY,
    value    TEXT NOT NULL
);

-- Curated (artist, track) identity per video, from `byp match`. Sync never
-- touches it. status: matched | not_found | manual.
CREATE TABLE IF NOT EXISTS track_metadata (
    video_id   TEXT PRIMARY KEY,
    artist     TEXT,
    track      TEXT,
    status     TEXT NOT NULL,
    source     TEXT NOT NULL,
    lastfm_url TEXT,
    mbid       TEXT,
    matched_at TEXT NOT NULL
);

-- Last.fm similarity cache, keyed by the seed's normalized (artist, track) so
-- duplicate uploads share it and a corrected match reads a fresh key.
-- similar_fetches records when a seed was fetched, even if it had no results.
CREATE TABLE IF NOT EXISTS similar_fetches (
    seed_artist TEXT NOT NULL,
    seed_track  TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (seed_artist, seed_track)
);

CREATE TABLE IF NOT EXISTS similar_tracks (
    seed_artist TEXT NOT NULL,
    seed_track  TEXT NOT NULL,
    artist      TEXT NOT NULL,
    track       TEXT NOT NULL,
    match       REAL NOT NULL,
    mbid        TEXT,
    lastfm_url  TEXT,
    PRIMARY KEY (seed_artist, seed_track, artist, track)
);

-- Artist-level Last.fm results (kind: similar | top_tracks) — the fallback
-- when a song has no track-level similarity. payload is a JSON list.
CREATE TABLE IF NOT EXISTS artist_cache (
    kind       TEXT NOT NULL,
    artist     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (kind, artist)
);

-- yt-dlp search results for songs outside the playlist, keyed by normalized
-- (artist, track). video_id NULL = searched, nothing found.
CREATE TABLE IF NOT EXISTS youtube_lookups (
    artist       TEXT NOT NULL,
    track        TEXT NOT NULL,
    video_id     TEXT,
    title        TEXT,
    channel      TEXT,
    looked_up_at TEXT NOT NULL,
    PRIMARY KEY (artist, track)
);

CREATE TABLE IF NOT EXISTS import_failures (
    target_playlist_id TEXT NOT NULL,
    video_id            TEXT NOT NULL,
    error                TEXT NOT NULL,
    failed_at            TEXT NOT NULL,
    PRIMARY KEY (target_playlist_id, video_id)
);
"""


def db_path() -> Path:
    return Path(os.environ.get("BYP_DB", "playlist.db"))


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to pre-existing databases created before a schema change."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(quota_log)")}
    if "project" not in columns:
        conn.execute("ALTER TABLE quota_log ADD COLUMN project TEXT NOT NULL DEFAULT 'default'")
        conn.commit()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlist_items)")}
    if "unavailable_at" not in columns:
        conn.execute("ALTER TABLE playlist_items ADD COLUMN unavailable_at TEXT")
        conn.commit()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM import_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO import_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pt_midnight_utc_iso() -> str:
    """Start of the current Pacific-time day, as a UTC timestamp string.

    Timestamps in ``quota_log`` are stored in this same ``...Z`` format, so a
    plain string comparison correctly selects "since the last reset".
    """
    now_pt = datetime.now(QUOTA_TZ)
    midnight_pt = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_pt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Quota:
    """Records API quota usage and reports how much is left for the PT day.

    ``project`` scopes usage to one GCP project's own daily quota — each
    project (its own client secret/token, see ``auth.py``) has an
    independent 10,000 unit/day budget.
    """

    def __init__(self, conn: sqlite3.Connection, project: str = "default") -> None:
        self.conn = conn
        self.project = project
        self.session_units = 0

    def charge(self, units: int, method: str) -> None:
        self.conn.execute(
            "INSERT INTO quota_log (ts, units, method, project) VALUES (?, ?, ?, ?)",
            (_utc_now_iso(), units, method, self.project),
        )
        self.conn.commit()
        self.session_units += units

    def used_today(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(units), 0) FROM quota_log WHERE ts >= ? AND project = ?",
            (_pt_midnight_utc_iso(), self.project),
        ).fetchone()
        return int(row[0])

    def remaining_today(self) -> int:
        return max(0, DAILY_QUOTA - self.used_today())
