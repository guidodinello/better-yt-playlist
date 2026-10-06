"""``similar`` (songs in the playlist like a seed) and ``discover`` (songs outside it).

Both start from Last.fm ``track.getSimilar`` for the seed's matched
(artist, track), cached for :data:`CACHE_TTL`. ``similar`` keeps the results
that are already in the playlist; ``discover`` keeps the rest and finds each
on YouTube with a yt-dlp search (zero API quota), cached in
``youtube_lookups``. Names are compared through :func:`.titles.norm` so
``"Song (Remastered)"`` and ``"song"`` meet.

Last.fm has no track-level similarity for many less-known songs (even ones
with tens of thousands of listeners), so when the track-level results come up
short of ``limit`` both commands fill the rest from **artist** similarity
(``artist.getSimilar``, starting with the seed's own artist): ``similar``
with playlist songs by those artists, ``discover`` with each artist's top
track (``artist.getTopTracks``). Those rows carry ``basis = "artist"``. An
artist name shared by several acts (e.g. "A.M.") makes this fallback
unreliable — Last.fm merges them into one profile.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .lastfm import LastfmApi, SimilarArtist, SimilarTrack, TrackInfo
from .match import MatchStatus, identify, store_result
from .titles import artist_candidates, norm

CACHE_TTL = timedelta(days=30)
# How many similar tracks to ask Last.fm for. Only a slice survives the
# playlist filter in `similar`, so ask wide.
_SIMILAR_FETCH_LIMIT = 250
_SIMILAR_ARTISTS_LIMIT = 50
_TOP_TRACKS_LIMIT = 5
_PER_ARTIST = 2
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


class Basis(StrEnum):
    TRACK = "track"  # Last.fm track.getSimilar
    ARTIST = "artist"  # fallback: artist.getSimilar (+ artist.getTopTracks)


@dataclass(frozen=True, slots=True)
class Recommendation:
    artist: str
    track: str
    match: float
    lastfm_url: str | None
    video_id: str | None
    title: str | None
    basis: Basis = Basis.TRACK


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


def _artist_key(name: str) -> str:
    return norm(name, "")[0]


def _cached_artist_call(
    conn: sqlite3.Connection,
    kind: str,
    artist: str,
    now: datetime,
    fetch: Callable[[], list[dict[str, object]]],
) -> list[dict[str, object]]:
    """``fetch()`` once per (kind, artist) per CACHE_TTL; rows stored as JSON."""
    key = _artist_key(artist)
    row = conn.execute(
        "SELECT fetched_at, payload FROM artist_cache WHERE kind = ? AND artist = ?", (kind, key)
    ).fetchone()
    if row is not None and row["fetched_at"] >= _ts(now - CACHE_TTL):
        return json.loads(row["payload"])
    payload = fetch()
    conn.execute(
        "INSERT OR REPLACE INTO artist_cache (kind, artist, fetched_at, payload) "
        "VALUES (?, ?, ?, ?)",
        (kind, key, _ts(now), json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return payload


def _related_artists(
    conn: sqlite3.Connection, seed: Seed, *, api: LastfmApi, now: datetime
) -> list[SimilarArtist]:
    """The seed's own artist (match 1.0), then Last.fm's similar artists."""
    rows = _cached_artist_call(
        conn,
        "similar",
        seed.artist,
        now,
        lambda: [
            {"name": a.name, "match": a.match}
            for a in api.similar_artists(seed.artist, limit=_SIMILAR_ARTISTS_LIMIT)
        ],
    )
    return [SimilarArtist(seed.artist, 1.0)] + [
        SimilarArtist(str(r["name"]), float(str(r["match"]))) for r in rows
    ]


def _top_tracks(
    conn: sqlite3.Connection, artist: str, *, api: LastfmApi, now: datetime
) -> list[TrackInfo]:
    rows = _cached_artist_call(
        conn,
        "top_tracks",
        artist,
        now,
        lambda: [
            {"artist": t.artist, "track": t.track, "url": t.url}
            for t in api.top_tracks(artist, limit=_TOP_TRACKS_LIMIT)
        ],
    )
    return [
        TrackInfo(str(r["artist"]), str(r["track"]), None, str(r["url"]) if r["url"] else None)
        for r in rows
    ]


@dataclass(slots=True)
class _PlaylistIndex:
    """Matched playlist songs by ``norm(artist, track)`` and by artist name, plus all video ids."""

    by_key: dict[tuple[str, str], tuple[str, str]] = field(
        default_factory=dict[tuple[str, str], tuple[str, str]]
    )
    by_artist: dict[str, list[tuple[str, str, str, str]]] = field(
        default_factory=dict[str, list[tuple[str, str, str, str]]]
    )
    video_ids: set[str] = field(default_factory=set[str])


def _playlist_index(conn: sqlite3.Connection, playlist_id: str) -> _PlaylistIndex:
    index = _PlaylistIndex()
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
        index.video_ids.add(r["video_id"])
        artist, track = r["artist"], r["track"]
        if not (artist and track):
            continue
        key = norm(artist, track)
        if key in index.by_key:
            continue  # another upload of a song already indexed
        index.by_key[key] = (r["video_id"], r["title"])
        # "Dave, Central Cee" is findable under each credited name's first form.
        for name in {_artist_key(a) for a in artist_candidates(artist)}:
            index.by_artist.setdefault(name, []).append((r["video_id"], r["title"], artist, track))
    return index


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
    now = now or datetime.now(UTC)
    index = _playlist_index(conn, playlist_id)
    seen = {norm(seed.artist, seed.track)}
    out: list[Recommendation] = []
    for t in similar_for(conn, seed, api=api, now=now):
        key = norm(t.artist, t.track)
        if key in seen or key not in index.by_key:
            continue
        seen.add(key)
        video_id, title = index.by_key[key]
        out.append(Recommendation(t.artist, t.track, t.match, t.url, video_id, title))
        if len(out) >= limit:
            return out

    # Best-matching artists first, but at most _PER_ARTIST songs each on the
    # first pass so one prolific artist doesn't fill the list; leftovers only
    # if it's still short.
    candidates: list[tuple[int, float, tuple[str, str, str, str]]] = []
    for related in _related_artists(conn, seed, api=api, now=now):
        # Drop already-listed songs (the seed, track-based results) first so
        # they don't use up the artist's slots.
        songs = [
            song
            for song in index.by_artist.get(_artist_key(related.name), [])
            if norm(song[2], song[3]) not in seen
        ]
        candidates += [(i // _PER_ARTIST, -related.match, song) for i, song in enumerate(songs)]
    # Stable sort: pass number, then match; ties keep artist and playlist order.
    for _, neg_match, (video_id, title, artist, track) in sorted(candidates, key=lambda c: c[:2]):
        key = norm(artist, track)
        if key in seen:
            continue
        seen.add(key)
        out.append(Recommendation(artist, track, -neg_match, None, video_id, title, Basis.ARTIST))
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
    index = _playlist_index(conn, playlist_id)
    seen = {norm(seed.artist, seed.track)}
    out: list[Recommendation] = []

    def consider(t: TrackInfo | SimilarTrack, match: float, basis: Basis) -> bool:
        """Add ``t`` if it's new and on YouTube; report whether it was added."""
        key = norm(t.artist, t.track)
        if key in seen or key in index.by_key:
            return False
        seen.add(key)
        hit = _lookup_youtube(conn, resolver, t.artist, t.track, now)
        # A name variant `norm` missed can still be the very video you have.
        if hit is None or hit.video_id in index.video_ids:
            return False
        out.append(Recommendation(t.artist, t.track, match, t.url, hit.video_id, hit.title, basis))
        return True

    for t in similar_for(conn, seed, api=api, now=now):
        consider(t, t.match, Basis.TRACK)
        if len(out) >= limit:
            return out

    # One top track per related artist, so the fill spans artists.
    for related in _related_artists(conn, seed, api=api, now=now):
        for t in _top_tracks(conn, related.name, api=api, now=now):
            if consider(t, related.match, Basis.ARTIST):
                break
        if len(out) >= limit:
            return out
    return out
