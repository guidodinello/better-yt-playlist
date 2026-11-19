"""Import the account's YouTube watch history into the local DB.

The Data API has no access to watch history, so this reads yt-dlp's history
feed (``:ythistory``) with browser cookies, the same way Watch Later is read.
It costs no quota. A machine without a browser (the Pi) loads a file instead:
yt-dlp's ``--dump-json`` output, written on a machine that has one.

Entries only accumulate: a video that drops out of the feed (Google's
auto-delete) keeps its row, with ``position`` cleared.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import connect

logger = logging.getLogger("byp")

HISTORY_FEED = ":ythistory"
# Thousands of entries, paged by yt-dlp; far slower than a single playlist.
FETCH_TIMEOUT_S = 1800
# Without an explicit end, yt-dlp (2026.10.21) read the whole feed for minutes,
# exited 0 and printed no entries; with one it printed all 20,733 (2026-11-19).
# Far above any real history.
PLAYLIST_END = 1_000_000

_UPSERT = """
INSERT INTO watch_history (
    video_id, title, channel_title, channel_id, duration_s, position,
    first_seen_at, last_seen_at
) VALUES (
    :video_id, :title, :channel_title, :channel_id, :duration_s, :position,
    :now, :now
)
ON CONFLICT(video_id) DO UPDATE SET
    title         = COALESCE(excluded.title, title),
    channel_title = COALESCE(excluded.channel_title, channel_title),
    channel_id    = COALESCE(excluded.channel_id, channel_id),
    duration_s    = COALESCE(excluded.duration_s, duration_s),
    position      = excluded.position,
    last_seen_at  = excluded.last_seen_at
"""


def ytdlp_history_cmd(browser: str) -> list[str]:
    """The yt-dlp command that dumps the history feed, one JSON entry per line."""
    return [
        "yt-dlp",
        "--cookies-from-browser",
        browser,
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--playlist-end",
        str(PLAYLIST_END),
        HISTORY_FEED,
    ]


def fetch_history_lines(browser: str) -> list[str]:
    """Run yt-dlp on the history feed; fail rather than return a partial read."""
    try:
        result = subprocess.run(
            ytdlp_history_cmd(browser), capture_output=True, text=True, timeout=FETCH_TIMEOUT_S
        )
    except FileNotFoundError:
        raise SystemExit("yt-dlp not found. Install it: uv tool install yt-dlp") from None
    except subprocess.TimeoutExpired:
        raise SystemExit("yt-dlp timed out reading the watch history") from None
    if result.returncode != 0:
        raise SystemExit(
            f"yt-dlp could not read the watch history from {browser} "
            f"(exit {result.returncode}). Is YouTube logged in there?\n"
            f"{result.stderr.strip()[-300:]}"
        )
    return result.stdout.splitlines()


def parse_entries(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Keep each video's most recent appearance, in feed order (newest first)."""
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = entry.get("id")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        duration = entry.get("duration")
        entries.append(
            {
                "video_id": video_id,
                "title": entry.get("title"),
                "channel_title": entry.get("channel") or entry.get("uploader"),
                "channel_id": entry.get("channel_id"),
                "duration_s": int(duration) if isinstance(duration, int | float) else None,
            }
        )
    return entries


def store_history(entries: list[dict[str, Any]], conn: sqlite3.Connection) -> dict[str, int]:
    """Upsert ``entries`` (newest first); clear ``position`` for videos no longer in the feed."""
    if not entries:
        # An empty read is a failed read, not an empty history: keep the table as it is.
        raise SystemExit("the watch history read came back empty; nothing changed.")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    before = conn.execute("SELECT count(*) FROM watch_history").fetchone()[0]
    conn.execute("UPDATE watch_history SET position = NULL")
    for position, entry in enumerate(entries):
        conn.execute(_UPSERT, {**entry, "position": position, "now": now})
    conn.commit()
    after = conn.execute("SELECT count(*) FROM watch_history").fetchone()[0]
    return {"videos": len(entries), "new": after - before, "total": after}


def import_history(
    browser: str | None = None,
    file: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """Read the history from ``browser`` (via yt-dlp) or from a saved ``file``, and store it."""
    if (browser is None) == (file is None):
        raise ValueError("pass exactly one of browser or file")
    if file is not None:
        lines = file.read_text(encoding="utf-8").splitlines()
    else:
        assert browser is not None
        logger.info("reading the watch history from %s (can take minutes) ...", browser)
        lines = fetch_history_lines(browser)
    return store_history(parse_entries(lines), conn or connect())
