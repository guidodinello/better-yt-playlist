"""CLI entry point.

    byp sync <playlist>          mirror a playlist into ./playlist.db
    byp query "<SQL>"            run DuckDB SQL over the mirror
    byp order-from-query "<SQL>" store a target order (SQL returns playlist_item_id)
    byp reorder [--budget N]     push the target order to YouTube, a bit at a time
    byp reorder --status         show how many moves and days remain
    byp clean [--playlist ID]    delete duplicate and unavailable entries, a bit at a time
    byp match                    map songs to Last.fm (artist, track)
    byp similar "<song>"         songs in the playlist similar to one of them
    byp discover "<song>"        similar songs not in the playlist, with YouTube links

Paths are overridable with BYP_DB, BYP_CLIENT_SECRET, BYP_TOKEN. The song
commands use BYP_SONGS_PLAYLIST (default playlist) and BYP_LASTFM_API_KEY.
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

    p_clean = sub.add_parser(
        "clean",
        help="Delete duplicate and deleted/private entries from a playlist (daily timer)",
    )
    p_clean.add_argument(
        "--playlist",
        default=os.environ.get("BYP_TARGET_PLAYLIST"),
        help="Playlist id to clean (default: $BYP_TARGET_PLAYLIST)",
    )

    songs_default = os.environ.get("BYP_SONGS_PLAYLIST")
    p_match = sub.add_parser("match", help="Map playlist songs to Last.fm (artist, track)")
    p_match.add_argument("--playlist", default=songs_default, help="(default: $BYP_SONGS_PLAYLIST)")
    p_match.add_argument(
        "--retry", action="store_true", help="Also retry songs previously not found"
    )
    p_match.add_argument(
        "--set",
        nargs=3,
        metavar=("VIDEO_ID", "ARTIST", "TRACK"),
        help="Pin one video's artist/track by hand (never overwritten by match)",
    )
    for name, help_text in (
        ("similar", "Songs in the playlist similar to a seed song (Last.fm)"),
        ("discover", "Similar songs NOT in the playlist, found on YouTube (Last.fm + yt-dlp)"),
    ):
        p_rec = sub.add_parser(name, help=help_text)
        p_rec.add_argument("seed", help="Video id, or text in the song's title/artist/track")
        p_rec.add_argument(
            "--playlist", default=songs_default, help="(default: $BYP_SONGS_PLAYLIST)"
        )
        p_rec.add_argument("--limit", type=int, default=10)
        p_rec.add_argument("--format", choices=("table", "json"), default="table", dest="fmt")

    args = parser.parse_args()

    if args.command in ("match", "similar", "discover"):
        if not args.playlist:
            raise SystemExit("no playlist — pass --playlist or set BYP_SONGS_PLAYLIST")
        from .lastfm import LastfmError

        try:
            _run_song_command(args)
        except LastfmError as exc:
            raise SystemExit(f"Last.fm: {exc}") from exc
        return 0

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
            "imported %(imported)d (already %(already)d, skipped %(skipped)d) of %(total)d "
            "Watch Later videos; %(failed)d permanently failed",
            stats,
        )
    elif args.command == "clean":
        if not args.playlist:
            raise SystemExit("no playlist — pass --playlist or set BYP_TARGET_PLAYLIST")
        from .clean import clean

        clean(args.playlist)

    return 0


def _run_song_command(args: argparse.Namespace) -> None:
    from .db import connect

    conn = connect()
    if args.command == "match":
        from .match import match_playlist, set_manual

        if args.set:
            set_manual(conn, *args.set)
            logger.info("pinned %s as %s — %s", *args.set)
            return
        stats = match_playlist(args.playlist, retry=args.retry, conn=conn)
        logger.info("matched %d, not found %d", stats.matched, stats.not_found)
        for line in stats.unmatched_titles:
            logger.info("  not found: %s", line)
        if stats.unmatched_titles:
            logger.info('fix one with: byp match --set VIDEO_ID "Artist" "Track"')
        return

    import json
    import sys
    from dataclasses import asdict

    from .lastfm import LastfmClient
    from .query import format_table
    from .similar import AmbiguousSeed, SeedError, YtDlpResolver, discover, resolve_seed, similar

    api = LastfmClient.from_env()
    try:
        seed = resolve_seed(conn, args.playlist, args.seed, api=api)
    except AmbiguousSeed as exc:
        lines = "\n".join(f"  {vid}  {title}" for vid, title in exc.candidates[:20])
        raise SystemExit(f"{exc} — be more specific, or pass a video id:\n{lines}") from exc
    except SeedError as exc:
        raise SystemExit(str(exc)) from exc

    if args.command == "similar":
        recs = similar(conn, args.playlist, seed, api=api, limit=args.limit)
    else:
        recs = discover(
            conn, args.playlist, seed, api=api, resolver=YtDlpResolver(), limit=args.limit
        )

    if args.fmt == "json":
        payload = {"seed": asdict(seed), "results": [asdict(r) for r in recs]}
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    print(f"Seed: {seed.artist} — {seed.track}\n")
    rows = [
        (
            f"{r.match:.2f}",
            r.artist,
            r.track,
            f"https://youtu.be/{r.video_id}" if r.video_id else "",
        )
        for r in recs
    ]
    sys.stdout.write(format_table(["match", "artist", "track", "youtube"], rows))
    if not recs:
        print("(nothing found)")
    print("\nSimilarity data: Last.fm")


if __name__ == "__main__":
    raise SystemExit(main())
