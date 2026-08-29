"""Import remaining Watch Later videos into the YouTube playlist.

Reads from the local DB (no yt-dlp re-download), respects daily quota.
Designed to run via systemd timer daily at midnight PT.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from better_yt_playlist.db import DAILY_QUOTA, Quota, connect
from better_yt_playlist.youtube import QuotaExceeded, insert_playlist_item

TARGET_PLAYLIST_ID = "PLH8xUbQjtTPc"
WL_PLAYLIST_ID = "WL"
INSERT_COST = 50
BUDGET = DAILY_QUOTA


def import_remaining(conn: sqlite3.Connection) -> dict:
    quota = Quota(conn)

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

    # Cap by budget
    available = BUDGET - quota.used_today()
    max_import = available // INSERT_COST
    if max_import <= 0:
        print(f"Quota exhausted ({quota.used_today()}/{BUDGET}). Nothing to import today.")
        return {"total": len(wl_ids), "already": len(imported_ids), "imported": 0, "skipped": 0}

    to_import = remaining[:max_import]
    print(
        f"Importing {len(to_import)} of {len(remaining)} remaining "
        f"videos ({available} quota units available)"
    )

    from better_yt_playlist.auth import get_client

    client = get_client()

    imported = 0
    skipped = 0

    for (vid,) in to_import:
        if quota.used_today() + INSERT_COST > BUDGET:
            print(f"Quota cap reached at {quota.used_today()} units.")
            break
        try:
            insert_playlist_item(client, quota, playlist_id=TARGET_PLAYLIST_ID, video_id=vid)
            imported += 1
            if imported % 50 == 0:
                print(f"  [{imported}/{len(to_import)}] imported")
        except QuotaExceeded:
            print("Quota exhausted mid-import.")
            break
        except Exception as exc:
            skipped += 1
            print(f"  skipped {vid}: {exc}")

    print(
        f"Done: {imported} imported, {skipped} skipped, "
        f"{len(remaining) - imported} still remaining."
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
