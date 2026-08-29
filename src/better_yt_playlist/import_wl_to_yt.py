"""Import remaining Watch Later videos into a target YouTube playlist.

Reads Watch Later rows from the local DB (no yt-dlp re-download), pushes the
ones not already imported to a target playlist via the Data API, and writes a
local row for each successful insert so the next run is idempotent. Designed to
run via systemd timer daily at midnight PT.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .db import DAILY_QUOTA, Quota, connect
from .youtube import QuotaExceeded, insert_playlist_item

logger = logging.getLogger("byp")

INSERT_COST = 50
WL_PLAYLIST_ID = "WL"


def import_remaining(
    target_playlist_id: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Push un-imported Watch Later videos to ``target_playlist_id``."""
    conn = conn or connect()
    quota = Quota(conn)

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

    # Cap by budget, accounting for quota already spent today.
    available = max(0, DAILY_QUOTA - quota.used_today())
    max_import = available // INSERT_COST
    if max_import <= 0:
        logger.info(
            "quota exhausted (%d/%d). Nothing to import today.",
            quota.used_today(),
            DAILY_QUOTA,
        )
        return {
            "total": len(wl_ids),
            "already": len(imported_ids),
            "imported": 0,
            "skipped": 0,
        }

    to_import = remaining[:max_import]
    logger.info(
        "importing %d of %d remaining videos (%d quota units available)",
        len(to_import),
        len(remaining),
        available,
    )

    from .auth import get_client

    client = get_client()

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    imported = 0
    skipped = 0

    for vid in to_import:
        if quota.used_today() + INSERT_COST > DAILY_QUOTA:
            logger.info("quota cap reached at %d units.", quota.used_today())
            break
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
            if imported % 50 == 0:
                logger.info("  [%d/%d] imported", imported, len(to_import))
        except QuotaExceeded:
            logger.info("quota exhausted mid-import.")
            break
        except Exception as exc:
            skipped += 1
            logger.warning("  skipped %s: %s", vid, exc)

    logger.info(
        "done: %d imported, %d skipped, %d still remaining.",
        imported,
        skipped,
        len(remaining) - imported,
    )
    return {
        "total": len(wl_ids),
        "already": len(imported_ids),
        "imported": imported,
        "skipped": skipped,
    }
