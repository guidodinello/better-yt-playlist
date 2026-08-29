"""CLI entry point.

    byp sync <playlist>          mirror a playlist into ./playlist.db
    byp query "<SQL>"            run DuckDB SQL over the mirror
    byp order-from-query "<SQL>" store a target order (SQL returns playlist_item_id)
    byp reorder [--budget N]     push the target order to YouTube, a bit at a time
    byp reorder --status         show how many moves and days remain

Paths are overridable with BYP_DB, BYP_CLIENT_SECRET, BYP_TOKEN.
"""

from __future__ import annotations

import argparse
import logging
import os

from . import __version__

logger = logging.getLogger("byp")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="byp", description="Better YT Playlist")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Mirror a YouTube playlist into local SQLite")
    p_sync.add_argument("playlist", help="Playlist id or any URL containing list=")

    p_query = sub.add_parser("query", help="Run DuckDB SQL against the local mirror")
    p_query.add_argument("sql")
    p_query.add_argument("--format", choices=("table", "csv", "json"), default="table", dest="fmt")

    p_order = sub.add_parser(
        "order-from-query",
        help="Store a target order; SQL must return playlist_item_id in desired order",
    )
    p_order.add_argument("sql")

    p_import = sub.add_parser(
        "import-watch-later",
        help="Import Watch Later via yt-dlp (local DB or YouTube API)",
    )
    p_import.add_argument(
        "--browser",
        default="chrome",
        choices=["chrome", "brave", "edge", "firefox"],
        help="Browser to read YouTube cookies from (default: chrome)",
    )
    p_import.add_argument("--name", default="Watch Later (Import)", help="New playlist name")
    p_import.add_argument(
        "--budget",
        type=int,
        default=10000,
        help="Max quota units to spend today (default 10000)",
    )
    p_import.add_argument(
        "--dry-run", action="store_true", help="Show what would happen, do nothing"
    )
    p_import.add_argument(
        "--api",
        action="store_true",
        help="Create a real playlist on YouTube via the API (default: local-only, zero quota)",
    )

    p_reorder = sub.add_parser("reorder", help="Push the stored target order to YouTube")
    p_reorder.add_argument(
        "--budget",
        type=int,
        default=9500,
        help="Max quota units to spend today (default 9500; daily cap is 10000)",
    )
    p_reorder.add_argument("--status", action="store_true", help="Show progress, do nothing")
    p_reorder.add_argument("--dry-run", action="store_true", help="List moves, do not apply")

    p_wl = sub.add_parser(
        "import-wl-to-yt",
        help="Import remaining Watch Later videos into a target YouTube playlist (daily timer)",
    )
    p_wl.add_argument(
        "--target-playlist",
        default=os.environ.get("BYP_TARGET_PLAYLIST"),
        help="Playlist id to import into (default: $BYP_TARGET_PLAYLIST)",
    )

    args = parser.parse_args()

    if args.command == "sync":
        from .sync import sync

        stats = sync(args.playlist)
        logger.info(
            "synced %(items)d items (%(videos_resolved)d videos resolved, "
            "%(dead_entries)d dead entries, %(newly_removed)d newly removed) "
            "— %(quota_spent)d quota units",
            stats,
        )
    elif args.command == "query":
        from .query import run_query

        run_query(args.sql, args.fmt)
    elif args.command == "order-from-query":
        from .query import fetch_column
        from .service import save_target_order

        save_target_order(fetch_column(args.sql))
    elif args.command == "import-watch-later":
        if args.api:
            from .import_watch_later import import_watch_later

            stats = import_watch_later(
                browser=args.browser,
                name=args.name,
                budget=args.budget,
                dry_run=args.dry_run,
            )
            if stats["imported"]:
                logger.info(
                    "imported %(imported)d of %(videos)d videos — %(quota_spent)d quota units",
                    stats,
                )
        else:
            from .import_watch_later import import_watch_later_local

            stats = import_watch_later_local(
                browser=args.browser,
                dry_run=args.dry_run,
            )
            if stats["inserted"]:
                logger.info(
                    "inserted %(inserted)d of %(videos)d videos into local DB — 0 quota units",
                    stats,
                )
    elif args.command == "reorder":
        from .service import reorder_status, run_reorder

        if args.status:
            reorder_status()
        else:
            run_reorder(budget=args.budget, dry_run=args.dry_run)
    elif args.command == "import-wl-to-yt":
        if not args.target_playlist:
            raise SystemExit(
                "no target playlist — pass --target-playlist or set BYP_TARGET_PLAYLIST"
            )
        from .import_wl_to_yt import import_remaining

        stats = import_remaining(args.target_playlist)
        logger.info(
            "imported %(imported)d (already %(already)d, skipped %(skipped)d) of %(total)d videos",
            stats,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
