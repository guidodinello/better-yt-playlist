"""Remove duplicate and unavailable entries from a YouTube playlist.

Two kinds of entry are removed:

- **Duplicates.** The Data API happily inserts a video into a playlist it is
  already in, and an early version of ``import-wl-to-yt`` did exactly that —
  re-pushing the same batch on several days — which filled the target playlist
  to YouTube's 5,000-item cap with extra copies. The earliest-added copy of
  each video is kept.
- **Unavailable videos** — deleted, or made private by their owner. These are
  the ones ``videos.list`` no longer returns (the same signal ``sync`` uses for
  ``dead_entries``); they still take a slot toward the cap.

Each run re-lists the live playlist (1 unit per 50 items) and looks up its
videos (1 unit per 50), then deletes entries (50 units each) until the quota
runs out. Re-running the next day picks up whatever is left, since the live
listing is the only state. Spends against each project in ``PROJECTS`` in turn,
the same way ``import-wl-to-yt`` does.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from .auth import AuthRequiredError, get_client
from .db import DAILY_QUOTA, Quota, connect
from .import_wl_to_yt import PROJECTS
from .youtube import (
    QuotaExceeded,
    delete_playlist_item,
    fetch_videos,
    iter_playlist_items,
)

logger = logging.getLogger("byp")

DELETE_COST = 50


def _video_id(item: dict[str, Any]) -> str:
    return item.get("contentDetails", {}).get("videoId") or item["snippet"]["resourceId"]["videoId"]


def find_duplicates(items: list[dict[str, Any]]) -> list[str]:
    """Return the ``playlist_item_id`` of every copy except each video's earliest-added one."""

    def added(item: dict[str, Any]) -> tuple[str, int]:
        snippet = item.get("snippet", {})
        return snippet.get("publishedAt", ""), snippet.get("position", 0)

    kept: set[str] = set()
    extra: list[str] = []
    for item in sorted(items, key=added):
        vid = _video_id(item)
        if vid in kept:
            extra.append(item["id"])
        else:
            kept.add(vid)
    return extra


def find_unavailable(items: list[dict[str, Any]], available: set[str]) -> list[str]:
    """Return the ``playlist_item_id`` of every entry whose video is not in ``available``."""
    return [item["id"] for item in items if _video_id(item) not in available]


def clean(playlist_id: str, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Delete duplicate and unavailable entries from ``playlist_id`` within today's quota."""
    conn = conn or connect()
    to_delete: list[str] | None = None
    deleted = 0

    for project in PROJECTS:
        quota = Quota(conn, project=project)
        if quota.remaining_today() < DELETE_COST:
            logger.info(
                "[%s] quota exhausted (%d/%d). skipping.", project, quota.used_today(), DAILY_QUOTA
            )
            continue
        try:
            client = get_client(project)
        except AuthRequiredError as exc:
            logger.warning("[%s] %s", project, exc)
            continue

        try:
            if to_delete is None:
                items = list(iter_playlist_items(client, playlist_id, quota))
                video_ids = sorted({_video_id(it) for it in items})
                available = set(fetch_videos(client, video_ids, quota))
                unavailable = find_unavailable(items, available)
                duplicates = find_duplicates(items)
                # A duplicated unavailable video shows up in both lists.
                to_delete = list(dict.fromkeys(unavailable + duplicates))
                logger.info(
                    "%d items: %d unavailable, %d duplicate copies — %d to delete",
                    len(items),
                    len(unavailable),
                    len(duplicates),
                    len(to_delete),
                )
            while deleted < len(to_delete) and quota.used_today() + DELETE_COST <= DAILY_QUOTA:
                delete_playlist_item(client, quota, to_delete[deleted])
                deleted += 1
                if deleted % 50 == 0:
                    logger.info("  [%d/%d] deleted", deleted, len(to_delete))
        except QuotaExceeded:
            logger.info("[%s] quota exhausted mid-clean.", project)
            continue

        if deleted >= len(to_delete):
            break

    total = len(to_delete or [])
    logger.info("clean: %d deleted, %d still remaining.", deleted, total - deleted)
    return {"found": total, "deleted": deleted, "remaining": total - deleted}
