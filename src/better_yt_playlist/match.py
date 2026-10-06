"""Map playlist videos to a canonical (artist, track) via Last.fm.

For each live, available video without a ``track_metadata`` row: parse the
title (:mod:`.titles`) and confirm each artist/track candidate with
``track.getInfo`` (autocorrect on), which also canonicalizes the names. If no
candidate confirms, fall back to ``track.search`` on the cleaned title and
take the hit with the most listeners — not the first hit: search ranks
scrobbles of the raw YouTube title (``"pelomusicgroup — MIRANDA! Fantasmas"``,
26 listeners, no similarity data) above the real track (``"Miranda! —
Fantasmas"``, ~35k). Below :data:`MIN_LISTENERS` nothing is taken,
and a hit only counts if every word of its (normalized) track name is in the
video title — search otherwise returns a popular *different* song
(``"The Vamp Is Back"`` → ``"The Bitch Is Back"``).

The same floor guards the parsed path: ``track.getInfo`` "confirms" any name
someone once scrobbled, so ``"Loser - Tame Impala (Spider-Man ...)"`` read as
artist *Loser* matches a 27-listener junk entry. A confirmation below the
floor is only a fallback: the reversed ``Track - Artist`` order and search
are tried first, and the junk entry is kept only if nothing better exists
(so genuinely niche songs still match).

A video nothing confirms is stored as ``not_found`` so later runs skip it
unless ``retry`` is set. ``manual`` rows are never touched.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from .db import connect
from .lastfm import LastfmApi, LastfmClient, TrackInfo
from .titles import (
    artist_candidates,
    clean_title,
    norm,
    parse_title,
    track_candidates,
    words_within,
)

logger = logging.getLogger("byp")

# Persist progress every N videos so an interrupted run keeps its work.
_COMMIT_EVERY = 25
MIN_LISTENERS = 1_000
_SEARCH_LIMIT = 10


class MatchStatus(StrEnum):
    MATCHED = "matched"
    NOT_FOUND = "not_found"
    MANUAL = "manual"


class MatchSource(StrEnum):
    PARSED = "parsed"
    LASTFM_SEARCH = "lastfm_search"
    MANUAL = "manual"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class MatchResult:
    info: TrackInfo | None
    source: MatchSource


@dataclass(slots=True)
class MatchStats:
    matched: int = 0
    not_found: int = 0
    unmatched_titles: list[str] = field(default_factory=list[str])


_UPSERT = """
INSERT INTO track_metadata
    (video_id, artist, track, status, source, lastfm_url, mbid, matched_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(video_id) DO UPDATE SET
    artist = excluded.artist, track = excluded.track, status = excluded.status,
    source = excluded.source, lastfm_url = excluded.lastfm_url, mbid = excluded.mbid,
    matched_at = excluded.matched_at
"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def identify(api: LastfmApi, title: str, channel_title: str | None) -> MatchResult:
    """Find the Last.fm track a video is, or ``MatchResult(None, NONE)``."""
    weak: MatchResult | None = None
    parsed = parse_title(title, channel_title)
    if parsed is not None:
        artist, track = parsed
        orders = [(artist, track), (track, artist)]
        for first_artist, first_track in orders:
            for a in artist_candidates(first_artist):
                for t in track_candidates(first_track):
                    info = api.track_info(a, t)
                    if info is None:
                        continue
                    if _is_established(info):
                        return MatchResult(info, MatchSource.PARSED)
                    weak = weak or MatchResult(info, MatchSource.PARSED)

    hits = [
        h
        for h in api.search_track(clean_title(title), limit=_SEARCH_LIMIT)
        if (h.listeners or 0) >= MIN_LISTENERS and words_within(norm("", h.track)[1], title)
    ]
    if hits:
        best = max(hits, key=lambda h: h.listeners or 0)
        info = api.track_info(best.artist, best.track)
        if info is not None:
            return MatchResult(info, MatchSource.LASTFM_SEARCH)
    return weak or MatchResult(None, MatchSource.NONE)


def _is_established(info: TrackInfo) -> bool:
    return (info.listeners or 0) >= MIN_LISTENERS


def store_result(conn: sqlite3.Connection, video_id: str, result: MatchResult) -> None:
    info = result.info
    status = MatchStatus.MATCHED if info is not None else MatchStatus.NOT_FOUND
    conn.execute(
        _UPSERT,
        (
            video_id,
            info.artist if info else None,
            info.track if info else None,
            status,
            result.source,
            info.url if info else None,
            info.mbid if info else None,
            _now(),
        ),
    )


def set_manual(conn: sqlite3.Connection, video_id: str, artist: str, track: str) -> None:
    """Pin a video's identity by hand; ``match`` never overwrites it."""
    exists = conn.execute(
        "SELECT 1 FROM playlist_items WHERE video_id = ? LIMIT 1", (video_id,)
    ).fetchone()
    if exists is None:
        raise SystemExit(f"no video {video_id!r} in the local mirror")
    conn.execute(
        _UPSERT,
        (video_id, artist, track, MatchStatus.MANUAL, MatchSource.MANUAL, None, None, _now()),
    )
    conn.commit()


def match_playlist(
    playlist_id: str,
    *,
    retry: bool = False,
    conn: sqlite3.Connection | None = None,
    api: LastfmApi | None = None,
) -> MatchStats:
    conn = conn or connect()
    api = api or LastfmClient.from_env()
    retry_clause = "OR m.status = 'not_found'" if retry else ""
    rows = conn.execute(
        f"""
        SELECT p.video_id, MIN(p.title) AS title, MIN(p.channel_title) AS channel_title
        FROM playlist_items p
        LEFT JOIN track_metadata m ON m.video_id = p.video_id
        WHERE p.playlist_id = ? AND p.removed_at IS NULL AND p.unavailable_at IS NULL
          AND (m.video_id IS NULL {retry_clause})
        GROUP BY p.video_id
        ORDER BY MIN(p.position)
        """,
        (playlist_id,),
    ).fetchall()
    logger.info("matching %d videos against Last.fm", len(rows))

    stats = MatchStats()
    try:
        for i, row in enumerate(rows, 1):
            result = identify(api, row["title"] or "", row["channel_title"])
            store_result(conn, row["video_id"], result)
            if result.info is not None:
                stats.matched += 1
            else:
                stats.not_found += 1
                stats.unmatched_titles.append(f"{row['video_id']}  {row['title']}")
            if i % _COMMIT_EVERY == 0:
                conn.commit()
                logger.info("  %d/%d", i, len(rows))
    finally:
        # Keep what's done even if Last.fm fails mid-run; a re-run resumes.
        conn.commit()
    return stats
