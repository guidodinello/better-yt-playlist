"""Import YouTube's Watch Later playlist.

Two modes:
  --local   (default) Read via yt-dlp, write straight to the local DB. Zero quota.
  --api               Read via yt-dlp, create a real YouTube playlist via the API.
                      Quota cost: 50 + 50 × N.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from datetime import UTC, datetime
from typing import Any

from .db import DAILY_QUOTA, Quota, connect
from .youtube import QuotaExceeded, create_playlist, insert_playlist_item

logger = logging.getLogger("byp")

INSERT_COST = 50
MAX_VIDEOS_PER_RUN = (DAILY_QUOTA - 50) // INSERT_COST  # 199


def fetch_watch_later_ids(browser: str = "chrome") -> list[str]:
    """Read Watch Later video IDs from the browser via yt-dlp."""
    cmd = [
        "yt-dlp",
        "--cookies-from-browser",
        browser,
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--ignore-errors",
        "https://www.youtube.com/playlist?list=WL",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise SystemExit("yt-dlp not found. Install it: pip install yt-dlp") from None
    except subprocess.TimeoutExpired:
        raise SystemExit("yt-dlp timed out reading Watch Later") from None

    ids: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = entry.get("id") or entry.get("url", "")
        if vid:
            ids.append(vid)

    if not ids and result.returncode != 0:
        stderr = result.stderr.strip()
        if "Unable to extract" in stderr or "cookies" in stderr.lower():
            raise SystemExit(
                f"yt-dlp could not read Watch Later from {browser}.\n"
                "Make sure you are logged into YouTube in that browser.\n"
                f"Error: {stderr[:200]}"
            )

    return ids


def _parse_duration(entry: dict[str, Any]) -> int | None:
    d = entry.get("duration")
    if d is None:
        return None
    try:
        return int(d)
    except (TypeError, ValueError):
        return None


def import_watch_later_local(
    browser: str = "chrome",
    playlist_id: str = "WL",
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Read Watch Later via yt-dlp and write directly to the local DB. Zero quota."""
    conn = conn or connect()

    logger.info("reading Watch Later from %s ...", browser)
    cmd = [
        "yt-dlp",
        "--cookies-from-browser",
        browser,
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--ignore-errors",
        "https://www.youtube.com/playlist?list=WL",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise SystemExit("yt-dlp not found. Install it: pip install yt-dlp") from None
    except subprocess.TimeoutExpired:
        raise SystemExit("yt-dlp timed out reading Watch Later") from None

    entries: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        logger.info("Watch Later is empty or unreadable.")
        return {"videos": 0, "inserted": 0, "skipped": 0}

    logger.info("found %d videos in Watch Later", len(entries))

    if dry_run:
        logger.info(
            "dry run: would insert %d videos into local DB as playlist '%s'",
            len(entries),
            playlist_id,
        )
        return {"videos": len(entries), "inserted": 0, "skipped": 0}

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    skipped = 0

    for i, entry in enumerate(entries):
        vid = entry.get("id") or entry.get("url", "")
        if not vid:
            skipped += 1
            continue

        # Use a deterministic playlist_item_id so re-runs are idempotent
        item_id = f"wl-{vid}"

        row = {
            "playlist_item_id": item_id,
            "playlist_id": playlist_id,
            "video_id": vid,
            "position": i,
            "title": entry.get("title"),
            "channel_title": entry.get("channel") or entry.get("uploader"),
            "channel_id": entry.get("channel_id"),
            "added_at": entry.get("upload_date"),
            "duration_s": _parse_duration(entry),
            "description": entry.get("description"),
            "tags": json.dumps(entry.get("tags")) if entry.get("tags") else None,
            "view_count": entry.get("view_count"),
            "published_at": entry.get("upload_date"),
            "synced_at": now,
        }

        conn.execute(
            """INSERT INTO playlist_items (
                playlist_item_id, playlist_id, video_id, position, title,
                channel_title, channel_id, added_at, duration_s, description,
                tags, view_count, published_at, synced_at, removed_at
            ) VALUES (
                :playlist_item_id, :playlist_id, :video_id, :position, :title,
                :channel_title, :channel_id, :added_at, :duration_s, :description,
                :tags, :view_count, :published_at, :synced_at, NULL
            )
            ON CONFLICT(playlist_item_id) DO UPDATE SET
                position = excluded.position,
                title = excluded.title,
                channel_title = excluded.channel_title,
                channel_id = excluded.channel_id,
                added_at = excluded.added_at,
                duration_s = excluded.duration_s,
                description = excluded.description,
                tags = excluded.tags,
                view_count = excluded.view_count,
                published_at = excluded.published_at,
                synced_at = excluded.synced_at,
                removed_at = NULL""",
            row,
        )
        inserted += 1
        if inserted % 500 == 0 or inserted == len(entries):
            logger.info("  [%d/%d] inserted", inserted, len(entries))

    conn.commit()
    logger.info("\ninserted %d videos, skipped %d — 0 quota units (local only).", inserted, skipped)
    logger.info(
        "Query it: byp query \"SELECT count(*) FROM playlist_items WHERE playlist_id = 'WL'\""
    )
    return {"videos": len(entries), "inserted": inserted, "skipped": skipped}


def import_watch_later(
    browser: str = "chrome",
    name: str = "Watch Later (Import)",
    budget: int = DAILY_QUOTA,
    dry_run: bool = False,
    client: Any | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Import Watch Later into a new playlist. Returns stats dict."""
    from .db import connect

    conn = conn or connect()
    quota = Quota(conn)

    # 1. Read Watch Later via yt-dlp
    logger.info("reading Watch Later from %s ...", browser)
    video_ids = fetch_watch_later_ids(browser)
    if not video_ids:
        logger.info("Watch Later is empty.")
        return {"videos": 0, "imported": 0, "skipped": 0, "quota_spent": 0}

    logger.info("found %d videos in Watch Later", len(video_ids))

    # 2. Check budget
    needed = INSERT_COST + INSERT_COST * len(video_ids)
    if not dry_run and needed > budget:
        capped = min(budget, DAILY_QUOTA)
        max_import = (capped - INSERT_COST) // INSERT_COST
        logger.info(
            "budget allows %d of %d videos today (%d units). Re-run to continue tomorrow.",
            max_import,
            len(video_ids),
            capped,
        )
        video_ids = video_ids[:max_import]
        needed = INSERT_COST + INSERT_COST * len(video_ids)

    if dry_run:
        logger.info("dry run: would create playlist '%s' with %d videos", name, len(video_ids))
        logger.info("  quota cost: %d units", needed)
        return {"videos": len(video_ids), "imported": 0, "skipped": 0, "quota_spent": 0}

    # 3. Create playlist
    if client is None:
        from .auth import get_client

        client = get_client()

    logger.info("creating playlist '%s' ...", name)
    playlist_id = create_playlist(client, quota, name, "Imported from Watch Later via byp")
    logger.info("created playlist %s", playlist_id)

    # 4. Insert videos
    imported = 0
    skipped = 0
    stopped: str | None = None

    try:
        for vid in video_ids:
            if quota.used_today() + INSERT_COST > min(budget, DAILY_QUOTA):
                stopped = "budget"
                break
            try:
                insert_playlist_item(client, quota, playlist_id=playlist_id, video_id=vid)
                imported += 1
                if imported % 10 == 0 or imported == len(video_ids):
                    logger.info("  [%d/%d] imported", imported, len(video_ids))
            except QuotaExceeded:
                stopped = "quota"
                logger.info("YouTube quota exhausted — stopping.")
                break
            except Exception as exc:
                skipped += 1
                logger.warning("  skipped %s: %s", vid, exc)
    except QuotaExceeded:
        stopped = "quota"

    logger.info(
        "\nimported %d videos, skipped %d, ~%d quota units used.",
        imported,
        skipped,
        quota.session_units,
    )
    if stopped == "budget":
        logger.info(
            "stopped at budget (%d units) — re-run `byp import-watch-later` to continue.",
            budget,
        )
    elif stopped == "quota":
        logger.info("re-run `byp import-watch-later` after quota resets (midnight PT).")
    elif imported < len(video_ids):
        logger.info("%d videos remain — re-run to continue.", len(video_ids) - imported)
    else:
        logger.info("done! Now sync it: byp sync %s", playlist_id)

    return {
        "videos": len(video_ids),
        "imported": imported,
        "skipped": skipped,
        "quota_spent": quota.session_units,
    }
