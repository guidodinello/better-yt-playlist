"""Import remaining Watch Later videos into the YouTube playlist.

Reads from the local DB (no yt-dlp re-download), respects daily quota.
Designed to run via systemd timer daily at midnight PT.

Spends against each project in PROJECTS in turn, falling through to the
next one once the current project's own daily quota is exhausted — each
project is a separate GCP project/OAuth client with its own 10,000 unit/day
budget (see auth.py's ``_paths_for``).
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from better_yt_playlist.auth import get_client
from better_yt_playlist.db import DAILY_QUOTA, Quota, connect
from better_yt_playlist.youtube import QuotaExceeded, insert_playlist_item

TARGET_PLAYLIST_ID = "PLH8xUbQjtTPc"
WL_PLAYLIST_ID = "WL"
INSERT_COST = 50
BUDGET = DAILY_QUOTA
PROJECTS = ["default", "2"]


def import_remaining(conn: sqlite3.Connection) -> dict:
    wl_ids = {
        r[0]
        for r in conn.execute(
            "SELECT video_id FROM playlist_items WHERE playlist_id = ?", (WL_PLAYLIST_ID,)
        ).fetchall()
    }

    imported_ids = {
        r[0]
        for r in conn.execute(
            "SELECT video_id FROM playlist_items WHERE playlist_id = ?", (TARGET_PLAYLIST_ID,)
        ).fetchall()
    }

    remaining = [
        vid
        for vid in conn.execute(
            "SELECT video_id FROM playlist_items WHERE playlist_id = ? ORDER BY position",
            (WL_PLAYLIST_ID,),
        ).fetchall()
        if vid[0] not in imported_ids
    ]

    if not remaining:
        print("Nothing to import — all Watch Later videos are already in the playlist.")
        return {"total": len(wl_ids), "already": len(imported_ids), "imported": 0, "skipped": 0}

    imported = 0
    skipped = 0
    idx = 0

    for project in PROJECTS:
        if idx >= len(remaining):
            break

        quota = Quota(conn, project=project)
        available = quota.remaining_today()
        if available < INSERT_COST:
            print(f"[{project}] quota exhausted ({quota.used_today()}/{BUDGET}). skipping.")
            continue

        print(f"[{project}] {available} quota units available")
        client = get_client(project)

        while idx < len(remaining) and quota.used_today() + INSERT_COST <= BUDGET:
            vid = remaining[idx][0]
            try:
                insert_playlist_item(client, quota, playlist_id=TARGET_PLAYLIST_ID, video_id=vid)
                imported += 1
                idx += 1
                if imported % 50 == 0:
                    print(f"  [{imported}/{len(remaining)}] imported")
            except QuotaExceeded:
                print(f"[{project}] quota exhausted mid-import.")
                break
            except Exception as exc:
                skipped += 1
                idx += 1
                print(f"  skipped {vid}: {exc}")

    print(
        f"Done: {imported} imported, {skipped} skipped, "
        f"{len(remaining) - imported - skipped} still remaining."
    )
    return {
        "total": len(wl_ids),
        "already": len(imported_ids),
        "imported": imported,
        "skipped": skipped,
    }


if __name__ == "__main__":
    conn = connect()
    import_remaining(conn)
