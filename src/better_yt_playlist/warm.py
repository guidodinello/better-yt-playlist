"""``warm``: build every song's page queue ahead of time, so the first tap is instant.

A tap on a song the page hasn't seen makes Last.fm calls and a yt-dlp search
per new song, about 20 s on the Raspberry Pi. ``warm`` builds the same queue
the page would (:func:`.play.seed_queue` with the page's defaults, then again
with "only new artists"), so those calls land in the caches the page reads:
``similar_tracks`` and ``artist_cache`` (30 days) and ``youtube_lookups``
(forever). It keeps no state of its own. A song whose lookups are all cached
costs no search, so each run picks up where the last stopped, and after the
first round only new songs (and Last.fm's 30-day refreshes) cost anything.

Unlike a tap, it isn't in a hurry: one search at a time with a pause after
each, so YouTube sees a trickle from this IP rather than bursts. A run stops
at its time limit, and early when searches fail several times in a row
(YouTube throttling looks like that).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .lastfm import LastfmApi
from .play import DEFAULT_LIMIT, DEFAULT_NEW, seed_queue
from .similar import Seed, YoutubeHit, YoutubeLookupFailed, YoutubeResolver
from .titles import norm

logger = logging.getLogger("byp")

MAX_FAILED_IN_A_ROW = 10
PAUSE_S = 1.0
_PROGRESS_EVERY = 50


class WarmStopped(Exception):
    """Stop the run: out of time, or searches keep failing."""


@dataclass(slots=True)
class WarmStats:
    songs: int = 0  # songs in the playlist (each warmed twice: all, then only new artists)
    warmed: int = 0  # (song, variant) queues built
    searches: int = 0
    failed: int = 0
    stopped: str | None = None


class _Paced:
    """Satisfies :class:`.similar.YoutubeResolver`: one search at a time, paced, time-boxed.

    ``discover`` calls it from worker threads; the lock makes them take turns.
    """

    def __init__(
        self,
        inner: YoutubeResolver,
        *,
        deadline: float,
        pause_s: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self._inner = inner
        self._deadline = deadline
        self._pause_s = pause_s
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._failed_in_a_row = 0
        self.searches = 0
        self.failed = 0

    def check(self) -> None:
        if self._clock() >= self._deadline:
            raise WarmStopped("time limit reached")
        if self._failed_in_a_row >= MAX_FAILED_IN_A_ROW:
            raise WarmStopped(f"{self._failed_in_a_row} searches failed in a row — throttled?")

    def resolve(self, artist: str, track: str) -> YoutubeHit | None:
        with self._lock:
            self.check()
            self.searches += 1
            try:
                hit = self._inner.resolve(artist, track)
            except YoutubeLookupFailed as exc:
                self.failed += 1
                self._failed_in_a_row += 1
                logger.info("search failed: %s", exc)
                raise
            else:
                self._failed_in_a_row = 0
                return hit
            finally:
                self._sleep(self._pause_s)


def _seeds(conn: sqlite3.Connection, playlist_id: str) -> list[Seed]:
    """Each matched, playable song once (its first upload), in playlist order."""
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
    max_minutes: float,
    pause_s: float = PAUSE_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> WarmStats:
    """Fill the caches a tap on each song reads, for every song in the playlist."""
    seeds = _seeds(conn, playlist_id)
    paced = _Paced(
        resolver,
        deadline=clock() + max_minutes * 60,
        pause_s=pause_s,
        sleep=sleep,
        clock=clock,
    )
    stats = WarmStats(songs=len(seeds))
    try:
        # Every song's default queue before any "only new artists" one: the
        # default is what a tap shows, so a short run covers it first.
        for new_artists in (False, True):
            for seed in seeds:
                paced.check()  # cached songs make no search, but may refresh Last.fm
                seed_queue(
                    conn,
                    playlist_id,
                    seed,
                    api=api,
                    limit=DEFAULT_LIMIT,
                    new=DEFAULT_NEW,
                    new_artists=new_artists,
                    resolver=paced,
                )
                stats.warmed += 1
                if stats.warmed % _PROGRESS_EVERY == 0:
                    logger.info(
                        "warmed %d of %d (%d searches)",
                        stats.warmed,
                        2 * len(seeds),
                        paced.searches,
                    )
    except WarmStopped as exc:
        stats.stopped = str(exc)
    stats.searches, stats.failed = paced.searches, paced.failed
    return stats
