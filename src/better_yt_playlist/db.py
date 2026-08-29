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
    removed_at       TEXT
);

CREATE TABLE IF NOT EXISTS target_order (
    playlist_id      TEXT NOT NULL,
    rank             INTEGER NOT NULL,
    playlist_item_id TEXT NOT NULL,
    PRIMARY KEY (playlist_id, rank)
);

CREATE TABLE IF NOT EXISTS quota_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    units  INTEGER NOT NULL,
    method TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_state (
    key      TEXT PRIMARY KEY,
    value    TEXT NOT NULL
);
"""


def db_path() -> Path:
    return Path(os.environ.get("BYP_DB", "playlist.db"))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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
    """Records API quota usage and reports how much is left for the PT day."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.session_units = 0

    def charge(self, units: int, method: str) -> None:
        self.conn.execute(
            "INSERT INTO quota_log (ts, units, method) VALUES (?, ?, ?)",
            (_utc_now_iso(), units, method),
        )
        self.conn.commit()
        self.session_units += units

    def used_today(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(units), 0) FROM quota_log WHERE ts >= ?",
            (_pt_midnight_utc_iso(),),
        ).fetchone()
        return int(row[0])

    def remaining_today(self) -> int:
        return max(0, DAILY_QUOTA - self.used_today())
