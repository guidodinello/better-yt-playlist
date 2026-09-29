"""Remove duplicate entries of the same video from a YouTube playlist.

The Data API happily inserts a video into a playlist it is already in, and an
early version of ``import-wl-to-yt`` did exactly that — re-pushing the same
batch on several days — which filled the target playlist to YouTube's
5,000-item cap with extra copies.

Each run re-lists the live playlist (1 unit per 50 items), keeps the
earliest-added copy of every video, and deletes the rest (50 units each) until
the quota runs out. Re-running the next day picks up whatever is left, since
the live listing is the only state. Spends against each project in
``PROJECTS`` in turn, the same way ``import-wl-to-yt`` does.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from .auth import AuthRequiredError, get_client
from .db import DAILY_QUOTA, Quota, connect
from .import_wl_to_yt import PROJECTS
from .youtube import QuotaExceeded, delete_playlist_item, iter_playlist_items

logger = logging.getLogger("byp")

DELETE_COST = 50


def find_duplicates(items: list[dict[str, Any]]) -> list[str]:
    """Return the ``playlist_item_id`` of every copy except each video's earliest-added one."""

    def added(item: dict[str, Any]) -> tuple[str, int]:
        snippet = item.get("snippet", {})
        return snippet.get("publishedAt", ""), snippet.get("position", 0)

    kept: set[str] = set()
    extra: list[str] = []
    for item in sorted(items, key=added):
        vid = (
            item.get("contentDetails", {}).get("videoId")
            or item["snippet"]["resourceId"]["videoId"]
        )
        if vid in kept:
            extra.append(item["id"])
        else:
            kept.add(vid)
    return extra


def dedupe(playlist_id: str, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Delete duplicate copies from ``playlist_id`` within today's quota."""
    conn = conn or connect()
    duplicates: list[str] | None = None
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
            if duplicates is None:
                items = list(iter_playlist_items(client, playlist_id, quota))
                duplicates = find_duplicates(items)
                logger.info("%d items, %d duplicate copies to delete", len(items), len(duplicates))
            while deleted < len(duplicates) and quota.used_today() + DELETE_COST <= DAILY_QUOTA:
                delete_playlist_item(client, quota, duplicates[deleted])
                deleted += 1
                if deleted % 50 == 0:
                    logger.info("  [%d/%d] deleted", deleted, len(duplicates))
        except QuotaExceeded:
            logger.info("[%s] quota exhausted mid-dedupe.", project)
            continue

        if deleted >= len(duplicates):
            break

    total = len(duplicates or [])
    logger.info("dedupe: %d deleted, %d still remaining.", deleted, total - deleted)
    return {"found": total, "deleted": deleted, "remaining": total - deleted}
