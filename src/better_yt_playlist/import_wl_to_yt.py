"""Import remaining Watch Later videos into a target YouTube playlist.

Reads Watch Later rows from the local DB (no yt-dlp re-download), pushes the
ones not already imported to a target playlist via the Data API, and writes a
local row for each successful insert so the next run is idempotent. Designed to
run via systemd timer daily at midnight PT.

Spends against each project in PROJECTS in turn, falling through to the next
one once the current project's own daily quota is exhausted — each project is
a separate GCP project/OAuth client with its own 10,000 unit/day budget (see
auth.py's ``_paths_for``). This only matters for the one-off Watch Later
backlog; a single project is plenty once it's caught up.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .auth import get_client
from .db import DAILY_QUOTA, Quota, connect
from .youtube import QuotaExceeded, insert_playlist_item

logger = logging.getLogger("byp")

INSERT_COST = 50
WL_PLAYLIST_ID = "WL"
PROJECTS = ["default", "2"]


def import_remaining(
    target_playlist_id: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Push un-imported Watch Later videos to ``target_playlist_id``."""
    conn = conn or connect()

    wl_ids = {
        r[0]
        for r in conn.execute(
            "SELECT video_id FROM playlist_items WHERE playlist_id = ?",
            (WL_PLAYLIST_ID,),
        ).fetchall()
    }

    imported_ids = {
        r[0]
        for r in conn.execute(
            "SELECT video_id FROM playlist_items WHERE playlist_id = ?",
            (target_playlist_id,),
        ).fetchall()
    }

    remaining = [
        r[0]
        for r in conn.execute(
            "SELECT video_id FROM playlist_items WHERE playlist_id = ? ORDER BY position",
            (WL_PLAYLIST_ID,),
        ).fetchall()
        if r[0] not in imported_ids
    ]

    if not remaining:
        logger.info("nothing to import — all Watch Later videos are already imported.")
        return {
            "total": len(wl_ids),
            "already": len(imported_ids),
            "imported": 0,
            "skipped": 0,
        }

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    imported = 0
    skipped = 0
    idx = 0

    for project in PROJECTS:
        if idx >= len(remaining):
            break

        quota = Quota(conn, project=project)
        available = quota.remaining_today()
        if available < INSERT_COST:
            logger.info(
                "[%s] quota exhausted (%d/%d). skipping.", project, quota.used_today(), DAILY_QUOTA
            )
            continue

        logger.info("[%s] %d quota units available", project, available)
        client = get_client(project)

        while idx < len(remaining) and quota.used_today() + INSERT_COST <= DAILY_QUOTA:
            vid = remaining[idx]
            try:
                insert_playlist_item(client, quota, playlist_id=target_playlist_id, video_id=vid)
                # Record the import locally so a re-run does not push it again.
                conn.execute(
                    "INSERT INTO playlist_items ("
                    "playlist_item_id, playlist_id, video_id, position, synced_at, removed_at"
                    ") VALUES (?, ?, ?, 0, ?, NULL) "
                    "ON CONFLICT(playlist_item_id) DO UPDATE SET synced_at = excluded.synced_at",
                    (f"{target_playlist_id}-{vid}", target_playlist_id, vid, now),
                )
                conn.commit()
                imported += 1
                idx += 1
                if imported % 50 == 0:
                    logger.info("  [%d/%d] imported", imported, len(remaining))
            except QuotaExceeded:
                logger.info("[%s] quota exhausted mid-import.", project)
                break
            except Exception as exc:
                skipped += 1
                idx += 1
                logger.warning("  skipped %s: %s", vid, exc)

    logger.info(
        "done: %d imported, %d skipped, %d still remaining.",
        imported,
        skipped,
        len(remaining) - imported - skipped,
    )
    return {
        "total": len(wl_ids),
        "already": len(imported_ids),
        "imported": imported,
        "skipped": skipped,
    }
