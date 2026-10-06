"""Seed the local mirror directly, for tests that start after a sync."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

PLAYLIST = "PLsongs"


def seed_playlist(
    conn: sqlite3.Connection,
    songs: Sequence[tuple[str, str, str | None]],
    *,
    playlist_id: str = PLAYLIST,
) -> None:
    """Insert ``(video_id, title, channel_title)`` rows as live playlist items, in order."""
    conn.executemany(
        "INSERT INTO playlist_items "
        "(playlist_item_id, playlist_id, video_id, position, title, channel_title, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        [
            (f"pi-{pos}-{vid}", playlist_id, vid, pos, title, channel)
            for pos, (vid, title, channel) in enumerate(songs)
        ],
    )
    conn.commit()
