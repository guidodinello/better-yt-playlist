"""``similar`` (songs in the playlist like a seed) and ``discover`` (songs outside it).

Both start from Last.fm ``track.getSimilar`` for the seed's matched
(artist, track), cached for :data:`CACHE_TTL`. ``similar`` keeps the results
that are already in the playlist; ``discover`` keeps the rest and finds each
on YouTube with a yt-dlp search (zero API quota), cached in
``youtube_lookups``. Names are compared through :func:`.titles.norm` so
``"Song (Remastered)"`` and ``"song"`` meet.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from .lastfm import LastfmApi, SimilarTrack
from .match import MatchStatus, identify, store_result
from .titles import norm

CACHE_TTL = timedelta(days=30)
# How many similar tracks to ask Last.fm for. Only a slice survives the
# playlist filter in `similar`, so ask wide.
_SIMILAR_FETCH_LIMIT = 250
_YTDLP_TIMEOUT_S = 60
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_IDENTIFIED = (MatchStatus.MATCHED, MatchStatus.MANUAL)


class SeedError(LookupError):
    """The seed query matched nothing usable."""


class AmbiguousSeed(SeedError):
    def __init__(self, query: str, candidates: Sequence[tuple[str, str]]) -> None:
        super().__init__(f"{query!r} matches {len(candidates)} songs")
        self.candidates = tuple(candidates)


@dataclass(frozen=True, slots=True)
class Seed:
    video_id: str
    title: str
    artist: str
    track: str


@dataclass(frozen=True, slots=True)
class Recommendation:
    artist: str
    track: str
    match: float
    lastfm_url: str | None
    video_id: str | None
    title: str | None


@dataclass(frozen=True, slots=True)
class YoutubeHit:
    video_id: str
    title: str
    channel: str


@runtime_checkable
class YoutubeResolver(Protocol):
    def resolve(self, artist: str, track: str) -> YoutubeHit | None: ...


class YtDlpResolver:
    """Satisfies :class:`YoutubeResolver` with a ``ytsearch1:`` yt-dlp call."""

    def resolve(self, artist: str, track: str) -> YoutubeHit | None:
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--no-warnings",
            "--print",
            "%(id)s\t%(title)s\t%(channel)s",
            f"ytsearch1:{artist} - {track}",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_YTDLP_TIMEOUT_S, check=False
            )
        except FileNotFoundError:
            raise SystemExit("yt-dlp not found. Install it: pip install yt-dlp") from None
        except subprocess.TimeoutExpired:
            return None
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0]:
            return None
        return YoutubeHit(video_id=parts[0], title=parts[1], channel=parts[2])


def _ts(now: datetime) -> str:
    return now.strftime(_TS_FORMAT)


def resolve_seed(conn: sqlite3.Connection, playlist_id: str, query: str, *, api: LastfmApi) -> Seed:
    """Pick the one playlist song ``query`` names (video id or title/artist/track text).

    A seed with no ``track_metadata`` yet is matched on the spot.
    """
    rows = conn.execute(
        """
        SELECT p.video_id, MIN(p.title) AS title, MIN(p.channel_title) AS channel_title,
               m.artist, m.track, m.status
        FROM playlist_items p
        LEFT JOIN track_metadata m ON m.video_id = p.video_id
        WHERE p.playlist_id = ? AND p.removed_at IS NULL
        GROUP BY p.video_id
        ORDER BY MIN(p.position)
        """,
        (playlist_id,),
    ).fetchall()
    # Filtered in Python: SQLite's lower() only folds ASCII ("ROSALÍA").
    needle = query.casefold()
    exact = [r for r in rows if r["video_id"] == query]
    rows = exact or [
        r
        for r in rows
        if needle in (r["title"] or "").casefold()
        or needle in f"{r['artist'] or ''} {r['track'] or ''}".casefold()
    ]
    if not rows:
        raise SeedError(f"no song in the playlist matches {query!r}")
    # Several uploads of one song ("Loser" official video + soundtrack upload)
    # aren't ambiguous: take the first.
    keys = {
        norm(r["artist"], r["track"]) if r["status"] in _IDENTIFIED else r["video_id"] for r in rows
    }
    if len(keys) > 1:
        raise AmbiguousSeed(query, [(r["video_id"], r["title"] or "") for r in rows])

    row = rows[0]
    if row["status"] in _IDENTIFIED:
        return Seed(row["video_id"], row["title"] or "", row["artist"], row["track"])

    result = identify(api, row["title"] or "", row["channel_title"])
    store_result(conn, row["video_id"], result)
    conn.commit()
    if result.info is None:
        raise SeedError(
            f"Last.fm doesn't know {row['title']!r} — set it by hand: "
            f'byp match --set {row["video_id"]} "Artist" "Track"'
        )
    return Seed(row["video_id"], row["title"] or "", result.info.artist, result.info.track)


def similar_for(
    conn: sqlite3.Connection, seed: Seed, *, api: LastfmApi, now: datetime | None = None
) -> list[SimilarTrack]:
    """Last.fm's similar tracks for ``seed``, from cache when fresher than CACHE_TTL."""
    now = now or datetime.now(UTC)
    key = norm(seed.artist, seed.track)
    fetched = conn.execute(
        "SELECT fetched_at FROM similar_fetches WHERE seed_artist = ? AND seed_track = ?", key
    ).fetchone()
    if fetched is not None and fetched["fetched_at"] >= _ts(now - CACHE_TTL):
        return [
            SimilarTrack(r["artist"], r["track"], r["match"], r["mbid"], r["lastfm_url"])
            for r in conn.execute(
                "SELECT * FROM similar_tracks WHERE seed_artist = ? AND seed_track = ? "
                "ORDER BY match DESC",
                key,
            )
        ]

    tracks = api.similar_tracks(seed.artist, seed.track, limit=_SIMILAR_FETCH_LIMIT)
    conn.execute("DELETE FROM similar_tracks WHERE seed_artist = ? AND seed_track = ?", key)
    conn.executemany(
        "INSERT OR IGNORE INTO similar_tracks "
        "(seed_artist, seed_track, artist, track, match, mbid, lastfm_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(*key, t.artist, t.track, t.match, t.mbid, t.url) for t in tracks],
    )
    conn.execute(
        "INSERT INTO similar_fetches (seed_artist, seed_track, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT DO UPDATE SET fetched_at = excluded.fetched_at",
        (*key, _ts(now)),
    )
    conn.commit()
    return tracks


def _playlist_songs(
    conn: sqlite3.Connection, playlist_id: str
) -> tuple[dict[tuple[str, str], tuple[str, str]], set[str]]:
    """``norm(artist, track) -> (video_id, title)`` for matched songs, plus all live video ids."""
    by_key: dict[tuple[str, str], tuple[str, str]] = {}
    video_ids: set[str] = set()
    for r in conn.execute(
        """
        SELECT p.video_id, p.title, m.artist, m.track
        FROM playlist_items p
        LEFT JOIN track_metadata m
               ON m.video_id = p.video_id AND m.status IN ('matched', 'manual')
        WHERE p.playlist_id = ? AND p.removed_at IS NULL
        ORDER BY p.position
        """,
        (playlist_id,),
    ):
        video_ids.add(r["video_id"])
        if r["artist"] and r["track"]:
            by_key.setdefault(norm(r["artist"], r["track"]), (r["video_id"], r["title"]))
    return by_key, video_ids


def similar(
    conn: sqlite3.Connection,
    playlist_id: str,
    seed: Seed,
    *,
    api: LastfmApi,
    limit: int,
    now: datetime | None = None,
) -> list[Recommendation]:
    """Playlist songs Last.fm rates similar to ``seed``, best first."""
    by_key, _ = _playlist_songs(conn, playlist_id)
    seed_key = norm(seed.artist, seed.track)
    out: list[Recommendation] = []
    seen: set[tuple[str, str]] = set()
    for t in similar_for(conn, seed, api=api, now=now):
        key = norm(t.artist, t.track)
        if key == seed_key or key in seen or key not in by_key:
            continue
        seen.add(key)
        video_id, title = by_key[key]
        out.append(Recommendation(t.artist, t.track, t.match, t.url, video_id, title))
        if len(out) >= limit:
            break
    return out


def _lookup_youtube(
    conn: sqlite3.Connection,
    resolver: YoutubeResolver,
    artist: str,
    track: str,
    now: datetime,
) -> YoutubeHit | None:
    key = norm(artist, track)
    cached = conn.execute(
        "SELECT video_id, title, channel FROM youtube_lookups WHERE artist = ? AND track = ?", key
    ).fetchone()
    if cached is not None:
        if cached["video_id"] is None:
            return None
        return YoutubeHit(cached["video_id"], cached["title"], cached["channel"])
    hit = resolver.resolve(artist, track)
    conn.execute(
        "INSERT OR REPLACE INTO youtube_lookups "
        "(artist, track, video_id, title, channel, looked_up_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            *key,
            hit.video_id if hit else None,
            hit.title if hit else None,
            hit.channel if hit else None,
            _ts(now),
        ),
    )
    conn.commit()
    return hit


def discover(
    conn: sqlite3.Connection,
    playlist_id: str,
    seed: Seed,
    *,
    api: LastfmApi,
    resolver: YoutubeResolver,
    limit: int,
    now: datetime | None = None,
) -> list[Recommendation]:
    """Songs similar to ``seed`` that are *not* in the playlist, each with a YouTube video."""
    now = now or datetime.now(UTC)
    by_key, playlist_video_ids = _playlist_songs(conn, playlist_id)
    seed_key = norm(seed.artist, seed.track)
    out: list[Recommendation] = []
    seen: set[tuple[str, str]] = set()
    for t in similar_for(conn, seed, api=api, now=now):
        key = norm(t.artist, t.track)
        if key == seed_key or key in seen or key in by_key:
            continue
        seen.add(key)
        hit = _lookup_youtube(conn, resolver, t.artist, t.track, now)
        # A name variant `norm` missed can still be the very video you have.
        if hit is None or hit.video_id in playlist_video_ids:
            continue
        out.append(Recommendation(t.artist, t.track, t.match, t.url, hit.video_id, hit.title))
        if len(out) >= limit:
            break
    return out
