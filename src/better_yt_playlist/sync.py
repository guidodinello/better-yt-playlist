"""Mirror a YouTube playlist into the local SQLite database."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .db import Quota, connect
from .youtube import fetch_videos, iter_playlist_items, parse_duration, parse_playlist_id

_UPSERT = """
INSERT INTO playlist_items (
    playlist_item_id, playlist_id, video_id, position, title, channel_title,
    channel_id, added_at, duration_s, description, tags, view_count,
    published_at, synced_at, removed_at
) VALUES (
    :playlist_item_id, :playlist_id, :video_id, :position, :title, :channel_title,
    :channel_id, :added_at, :duration_s, :description, :tags, :view_count,
    :published_at, :synced_at, NULL
)
ON CONFLICT(playlist_item_id) DO UPDATE SET
    position     = excluded.position,
    title        = excluded.title,
    channel_title= excluded.channel_title,
    channel_id   = excluded.channel_id,
    added_at     = excluded.added_at,
    duration_s   = excluded.duration_s,
    description  = excluded.description,
    tags         = excluded.tags,
    view_count   = excluded.view_count,
    published_at = excluded.published_at,
    synced_at    = excluded.synced_at,
    removed_at   = NULL
"""


def _video_id(item: dict[str, Any]) -> str:
    details = item.get("contentDetails", {})
    snippet = item.get("snippet", {})
    return details.get("videoId") or snippet.get("resourceId", {}).get("videoId", "")


def _row(
    item: dict[str, Any], playlist_id: str, videos: dict[str, dict[str, Any]], now: str
) -> dict[str, Any]:
    snippet = item.get("snippet", {})
    video_id = _video_id(item)

    video = videos.get(video_id, {})
    v_snippet = video.get("snippet", {})
    v_details = video.get("contentDetails", {})
    v_stats = video.get("statistics", {})

    tags = v_snippet.get("tags")
    view_count = v_stats.get("viewCount")

    return {
        "playlist_item_id": item["id"],
        "playlist_id": playlist_id,
        "video_id": video_id,
        "position": snippet.get("position", 0),
        "title": snippet.get("title"),
        # These two are absent for deleted / private videos — that is the signal.
        "channel_title": snippet.get("videoOwnerChannelTitle"),
        "channel_id": snippet.get("videoOwnerChannelId"),
        "added_at": snippet.get("publishedAt"),
        "duration_s": parse_duration(v_details.get("duration")),
        "description": v_snippet.get("description"),
        "tags": json.dumps(tags) if tags else None,
        "view_count": int(view_count) if view_count is not None else None,
        "published_at": v_snippet.get("publishedAt"),
        "synced_at": now,
    }


def sync(
    playlist: str, conn: sqlite3.Connection | None = None, client: Any | None = None
) -> dict[str, int]:
    """Fetch the playlist and upsert it. Returns a small stats dict."""
    playlist_id = parse_playlist_id(playlist)
    conn = conn or connect()
    if client is None:
        from .auth import get_client

        client = get_client()
    quota = Quota(conn)

    items = list(iter_playlist_items(client, playlist_id, quota))
    video_ids = sorted({vid for i in items if (vid := _video_id(i))})
    videos = fetch_videos(client, video_ids, quota)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen: list[str] = []
    dead = 0
    for item in items:
        row = _row(item, playlist_id, videos, now)
        seen.append(row["playlist_item_id"])
        dead += row["video_id"] not in videos
        conn.execute(_UPSERT, row)

    placeholders = ",".join("?" * len(seen)) or "NULL"
    removed = conn.execute(
        f"UPDATE playlist_items SET removed_at = ? "
        f"WHERE playlist_id = ? AND removed_at IS NULL "
        f"AND playlist_item_id NOT IN ({placeholders})",
        [now, playlist_id, *seen],
    ).rowcount
    conn.commit()

    return {
        "items": len(items),
        "videos_resolved": len(videos),
        "dead_entries": dead,
        "newly_removed": removed,
        "quota_spent": quota.session_units,
    }
