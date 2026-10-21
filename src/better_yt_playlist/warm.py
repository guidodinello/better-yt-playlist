"""``warm``: run ``discover`` for every song ahead of time, so the page's first tap is instant.

A tap on a song the page hasn't seen runs Last.fm calls and a yt-dlp search per
new song — about 20 s on the Raspberry Pi. ``warm`` makes the same calls the
page would (``DEFAULT_NEW`` new songs, then the "only new artists" variant), so
they land in the caches ``discover`` already reads: ``similar_tracks`` and
``artist_cache`` (30 days) and ``youtube_lookups`` (forever). No state of its
own: a song whose lookups are all cached costs no search, so each run picks up
where the last one stopped, and after the first nights only new songs (and
Last.fm's 30-day refreshes) cost anything.

Each run stops after ``max_searches`` yt-dlp searches, and early if searches
keep failing — YouTube throttling a burst of searches looks like that — so one
night never hammers YouTube.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from .lastfm import LastfmApi
from .play import DEFAULT_NEW
from .similar import Seed, YoutubeHit, YoutubeLookupFailed, YoutubeResolver, discover
from .titles import norm

logger = logging.getLogger("byp")

MAX_FAILED_SEARCHES = 20
_PROGRESS_EVERY = 100


class WarmStopped(Exception):
    """Stop the run: the search budget is spent, or searches keep failing."""


@dataclass(slots=True)
class WarmStats:
    songs: int = 0  # songs in the playlist (each warmed twice: all, then only new artists)
    warmed: int = 0  # (song, variant) runs that finished
    searches: int = 0
    failed: int = 0
    stopped: str | None = None


class _Budgeted:
    """Satisfies :class:`.similar.YoutubeResolver`; counts searches and stops the run.

    Called from ``discover``'s worker threads, hence the lock.
    """

    def __init__(self, inner: YoutubeResolver, max_searches: int) -> None:
        self._inner = inner
        self._max = max_searches
        self._lock = threading.Lock()
        self.searches = 0
        self.failed = 0

    def resolve(self, artist: str, track: str) -> YoutubeHit | None:
        with self._lock:
            if self.searches >= self._max:
                raise WarmStopped(f"search budget of {self._max} spent")
            if self.failed >= MAX_FAILED_SEARCHES:
                raise WarmStopped(f"{self.failed} searches failed — YouTube throttling?")
            self.searches += 1
        try:
            return self._inner.resolve(artist, track)
        except YoutubeLookupFailed as exc:
            with self._lock:
                self.failed += 1
            logger.info("search failed: %s", exc)
            raise


def _seeds(conn: sqlite3.Connection, playlist_id: str) -> list[Seed]:
    """Each matched, playable song once (the first upload of a song), in playlist order."""
    seeds: dict[tuple[str, str], Seed] = {}
    for r in conn.execute(
        """
        SELECT p.video_id, MIN(p.title) AS title, m.artist, m.track
        FROM playlist_items p
        JOIN track_metadata m ON m.video_id = p.video_id AND m.status IN ('matched', 'manual')
        WHERE p.playlist_id = ? AND p.removed_at IS NULL AND p.unavailable_at IS NULL
        GROUP BY p.video_id
        ORDER BY MIN(p.position)
        """,
        (playlist_id,),
    ):
        seeds.setdefault(
            norm(r["artist"], r["track"]),
            Seed(r["video_id"], r["title"] or "", r["artist"], r["track"]),
        )
    return list(seeds.values())


def warm(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    api: LastfmApi,
    resolver: YoutubeResolver,
    max_searches: int,
    now: datetime | None = None,
) -> WarmStats:
    """Fill the caches the page's new songs come from, for every song in the playlist."""
    now = now or datetime.now(UTC)
    seeds = _seeds(conn, playlist_id)
    budgeted = _Budgeted(resolver, max_searches)
    stats = WarmStats(songs=len(seeds))
    try:
        # All songs' default queues before any "only new artists" one: the default
        # is what a tap shows, so a budget that runs out covers it first.
        for new_artists in (False, True):
            for seed in seeds:
                discover(
                    conn,
                    playlist_id,
                    seed,
                    api=api,
                    resolver=budgeted,
                    limit=DEFAULT_NEW,
                    new_artists=new_artists,
                    now=now,
                )
                stats.warmed += 1
                if stats.warmed % _PROGRESS_EVERY == 0:
                    logger.info(
                        "warmed %d of %d (%d searches)",
                        stats.warmed,
                        2 * len(seeds),
                        budgeted.searches,
                    )
    except WarmStopped as exc:
        stats.stopped = str(exc)
    stats.searches, stats.failed = budgeted.searches, budgeted.failed
    return stats
