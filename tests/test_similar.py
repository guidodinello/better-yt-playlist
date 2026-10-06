"""similar / discover: seed resolution, playlist join, exclusion, and caching."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from support.fakes import FakeLastfm, FakeResolver
from support.playlist import PLAYLIST, seed_playlist

from better_yt_playlist import db
from better_yt_playlist.lastfm import SimilarArtist, SimilarTrack, TrackInfo
from better_yt_playlist.match import set_manual
from better_yt_playlist.similar import (
    AmbiguousSeed,
    Basis,
    SeedError,
    YoutubeHit,
    discover,
    resolve_seed,
    similar,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
LOSER = TrackInfo("Tame Impala", "Loser", None, None)


def _sim(artist: str, track: str, match: float) -> SimilarTrack:
    return SimilarTrack(artist, track, match, None, f"https://last.fm/{track}")


@pytest.fixture
def conn(tmp_path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "s.db"))
    conn = db.connect()
    seed_playlist(
        conn,
        [
            ("vLoser", "Tame Impala - Loser (Official Video)", "tameimpalaVEVO"),
            ("vRoll", "Adele - Rolling in the Deep (Official Music Video)", "Adele"),
            ("vLost", "Tame Impala - Lost In Yesterday (Official Video)", "tameimpalaVEVO"),
            ("vHello", "Adele - Hello", "Adele"),
        ],
    )
    for vid, artist, track in [
        ("vRoll", "Adele", "Rolling in the Deep"),
        ("vLost", "Tame Impala", "Lost in Yesterday"),
        ("vHello", "Adele", "Hello"),
    ]:
        set_manual(conn, vid, artist, track)
    return conn


def test_resolve_seed_matches_unmatched_seed_on_the_spot(conn: sqlite3.Connection) -> None:
    seed = resolve_seed(conn, PLAYLIST, "loser", api=FakeLastfm(known=[LOSER]))
    assert (seed.video_id, seed.artist, seed.track) == ("vLoser", "Tame Impala", "Loser")
    row = conn.execute("SELECT status FROM track_metadata WHERE video_id = 'vLoser'").fetchone()
    assert row["status"] == "matched"


def test_resolve_seed_by_video_id_and_by_artist(conn: sqlite3.Connection) -> None:
    assert resolve_seed(conn, PLAYLIST, "vHello", api=FakeLastfm()).track == "Hello"
    assert resolve_seed(conn, PLAYLIST, "rolling", api=FakeLastfm()).video_id == "vRoll"


def test_resolve_seed_ambiguous_lists_candidates(conn: sqlite3.Connection) -> None:
    with pytest.raises(AmbiguousSeed) as exc:
        resolve_seed(conn, PLAYLIST, "adele", api=FakeLastfm())
    assert [vid for vid, _ in exc.value.candidates] == ["vRoll", "vHello"]


def test_resolve_seed_two_uploads_of_one_song_are_not_ambiguous(
    conn: sqlite3.Connection,
) -> None:
    seed_playlist(
        conn,
        [("vRoll2", "Rolling in the Deep - Adele (Live at the Royal Albert Hall)", "x")],
        playlist_id=PLAYLIST,
    )
    conn.execute("UPDATE playlist_items SET position = 99 WHERE video_id = 'vRoll2'")
    set_manual(conn, "vRoll2", "Adele", "Rolling in the Deep (Live)")
    assert resolve_seed(conn, PLAYLIST, "rolling", api=FakeLastfm()).video_id == "vRoll"


def test_resolve_seed_none_or_unknown_to_lastfm(conn: sqlite3.Connection) -> None:
    with pytest.raises(SeedError, match="no song"):
        resolve_seed(conn, PLAYLIST, "nirvana", api=FakeLastfm())
    with pytest.raises(SeedError, match="match --set"):
        resolve_seed(conn, PLAYLIST, "loser", api=FakeLastfm())


def _api_with_similar(*tracks: SimilarTrack) -> FakeLastfm:
    return FakeLastfm(known=[LOSER], similar={("tame impala", "loser"): list(tracks)})


def test_similar_keeps_only_playlist_songs_under_name_variants(conn: sqlite3.Connection) -> None:
    api = _api_with_similar(
        _sim("Tame Impala", "Lost in Yesterday (Remastered)", 0.9),
        _sim("MGMT", "Electric Feel", 0.8),  # not in the playlist
        _sim("ADELE", "Hello - Live", 0.4),
        _sim("Tame Impala", "Loser", 0.3),  # the seed itself
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    recs = similar(conn, PLAYLIST, seed, api=api, limit=10, now=NOW)
    assert [(r.video_id, r.match) for r in recs] == [("vLost", 0.9), ("vHello", 0.4)]


def test_similar_respects_limit(conn: sqlite3.Connection) -> None:
    api = _api_with_similar(
        _sim("Tame Impala", "Lost in Yesterday", 0.9), _sim("Adele", "Hello", 0.4)
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    assert len(similar(conn, PLAYLIST, seed, api=api, limit=1, now=NOW)) == 1


def test_discover_excludes_playlist_songs_by_name_and_by_video(conn: sqlite3.Connection) -> None:
    api = _api_with_similar(
        _sim("MGMT", "Electric Feel", 0.9),
        _sim("Tame Impala", "Lost in Yesterday", 0.8),  # in playlist by name
        _sim("Tame Impala", "Lost Yesterday", 0.7),  # same video, name norm misses
        _sim("Nobody", "Unfindable", 0.6),  # yt-dlp finds nothing
        _sim("Pond", "Sitting Up on Our Crane", 0.5),
    )
    resolver = FakeResolver(
        hits={
            "MGMT - Electric Feel": YoutubeHit("vMGMT", "MGMT - Electric Feel", "MGMT"),
            "Tame Impala - Lost Yesterday": YoutubeHit("vLost", "t", "c"),
            "Pond - Sitting Up on Our Crane": YoutubeHit("vPond", "Pond - Crane", "Pond"),
        }
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    recs = discover(conn, PLAYLIST, seed, api=api, resolver=resolver, limit=10, now=NOW)
    assert [r.video_id for r in recs] == ["vMGMT", "vPond"]
    assert "Tame Impala - Lost in Yesterday" not in resolver.calls


def test_similarity_and_youtube_lookups_are_cached(conn: sqlite3.Connection) -> None:
    api = _api_with_similar(_sim("MGMT", "Electric Feel", 0.9))
    resolver = FakeResolver(hits={"MGMT - Electric Feel": YoutubeHit("vMGMT", "t", "c")})
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)

    first = discover(conn, PLAYLIST, seed, api=api, resolver=resolver, limit=5, now=NOW)
    again = discover(
        conn, PLAYLIST, seed, api=api, resolver=resolver, limit=5, now=NOW + timedelta(days=29)
    )
    assert first == again
    assert (api.similar_calls, len(resolver.calls)) == (1, 1)

    discover(
        conn, PLAYLIST, seed, api=api, resolver=resolver, limit=5, now=NOW + timedelta(days=31)
    )
    assert api.similar_calls == 2


def test_empty_similarity_result_is_cached_too(conn: sqlite3.Connection) -> None:
    api = _api_with_similar()
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    similar(conn, PLAYLIST, seed, api=api, limit=5, now=NOW)
    similar(conn, PLAYLIST, seed, api=api, limit=5, now=NOW)
    assert api.similar_calls == 1


def test_manual_correction_reads_a_fresh_cache_key(conn: sqlite3.Connection) -> None:
    api = FakeLastfm(
        known=[LOSER],
        similar={
            ("tame impala", "loser"): [_sim("Adele", "Hello", 0.5)],
            ("beck", "loser"): [_sim("Tame Impala", "Lost in Yesterday", 0.7)],
        },
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    assert [r.video_id for r in similar(conn, PLAYLIST, seed, api=api, limit=1, now=NOW)] == [
        "vHello"
    ]

    set_manual(conn, "vLoser", "Beck", "Loser")
    seed = resolve_seed(conn, PLAYLIST, "vLoser", api=api)
    assert [r.video_id for r in similar(conn, PLAYLIST, seed, api=api, limit=1, now=NOW)] == [
        "vLost"
    ]


def test_similar_fills_with_artist_similarity_when_no_track_data(
    conn: sqlite3.Connection,
) -> None:
    # Real shape: Last.fm has no track.getSimilar data for the seed, but does
    # for its artist (Zeballos → La Nueva Escuela, ...).
    seed_playlist(
        conn,
        [
            ("vZeb", "LO QUE SOY - ZEBALLOS", "Zeballos 17"),
            ("vZeb2", "Zeballos - Otra", "x"),
            ("vLNE", "La Nueva Escuela - Qué Dolor (Video Oficial)", "x"),
        ],
    )
    for vid, artist, track in [
        ("vZeb", "Zeballos", "Lo Que Soy"),
        ("vZeb2", "Zeballos", "Otra"),
        ("vLNE", "La Nueva Escuela", "Que Dolor"),
    ]:
        set_manual(conn, vid, artist, track)
    api = FakeLastfm(
        similar_artist_map={
            "zeballos": [SimilarArtist("La Nueva Escuela", 0.8), SimilarArtist("Wos", 0.5)]
        }
    )
    seed = resolve_seed(conn, PLAYLIST, "vZeb", api=api)

    recs = similar(conn, PLAYLIST, seed, api=api, limit=10, now=NOW)

    # Seed's own artist first (match 1.0), then similar artists; all artist-based.
    assert [(r.video_id, r.match, r.basis) for r in recs] == [
        ("vZeb2", 1.0, Basis.ARTIST),
        ("vLNE", 0.8, Basis.ARTIST),
    ]


def test_track_results_come_before_artist_fill_without_duplicates(
    conn: sqlite3.Connection,
) -> None:
    api = FakeLastfm(
        known=[LOSER],
        similar={("tame impala", "loser"): [_sim("Adele", "Hello", 0.5)]},
        similar_artist_map={"tame impala": [SimilarArtist("Adele", 0.3)]},
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    recs = similar(conn, PLAYLIST, seed, api=api, limit=10, now=NOW)
    assert [(r.video_id, r.basis) for r in recs] == [
        ("vHello", Basis.TRACK),
        ("vLost", Basis.ARTIST),  # seed's own artist
        ("vRoll", Basis.ARTIST),  # Adele, minus Hello already listed
    ]


def test_no_artist_fill_when_track_results_reach_the_limit(conn: sqlite3.Connection) -> None:
    api = _api_with_similar(_sim("Adele", "Hello", 0.5))
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    similar(conn, PLAYLIST, seed, api=api, limit=1, now=NOW)
    assert api.artist_calls == 0


def test_discover_fills_with_one_top_track_per_similar_artist(conn: sqlite3.Connection) -> None:
    api = FakeLastfm(
        known=[LOSER],
        similar_artist_map={
            "tame impala": [SimilarArtist("MGMT", 0.9), SimilarArtist("Pond", 0.7)]
        },
        top={
            "tame impala": [TrackInfo("Tame Impala", "Lost in Yesterday", None, None)],  # have it
            "mgmt": [
                TrackInfo("MGMT", "Electric Feel", None, None),
                TrackInfo("MGMT", "Kids", None, None),
            ],
            "pond": [TrackInfo("Pond", "Sitting Up on Our Crane", None, None)],
        },
    )
    resolver = FakeResolver(
        hits={
            "MGMT - Electric Feel": YoutubeHit("vMGMT", "t", "c"),
            "MGMT - Kids": YoutubeHit("vKids", "t", "c"),
            "Pond - Sitting Up on Our Crane": YoutubeHit("vPond", "t", "c"),
        }
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    recs = discover(conn, PLAYLIST, seed, api=api, resolver=resolver, limit=10, now=NOW)
    assert [(r.video_id, r.match, r.basis) for r in recs] == [
        ("vMGMT", 0.9, Basis.ARTIST),
        ("vPond", 0.7, Basis.ARTIST),
    ]


def test_artist_calls_are_cached(conn: sqlite3.Connection) -> None:
    api = FakeLastfm(known=[LOSER], similar_artist_map={"tame impala": [SimilarArtist("X", 0.5)]})
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    similar(conn, PLAYLIST, seed, api=api, limit=10, now=NOW)
    similar(conn, PLAYLIST, seed, api=api, limit=10, now=NOW + timedelta(days=1))
    assert api.artist_calls == 1


def test_artist_fill_caps_songs_per_artist_before_leftovers(conn: sqlite3.Connection) -> None:
    seed_playlist(
        conn,
        [
            ("vRoll2", "Adele - Skyfall", "x"),
            ("vLost2", "Tame Impala - Elephant", "x"),
            ("vLost3", "Tame Impala - Borderline", "x"),
        ],
    )
    set_manual(conn, "vRoll2", "Adele", "Skyfall")
    set_manual(conn, "vLost2", "Tame Impala", "Elephant")
    set_manual(conn, "vLost3", "Tame Impala", "Borderline")
    api = FakeLastfm(
        known=[LOSER], similar_artist_map={"tame impala": [SimilarArtist("Adele", 0.3)]}
    )
    seed = resolve_seed(conn, PLAYLIST, "vLoser", api=api)
    recs = similar(conn, PLAYLIST, seed, api=api, limit=10, now=NOW)
    # Two Tame Impala (match 1.0), two Adele (0.3), then the leftovers.
    assert [(r.artist, r.match) for r in recs] == [
        ("Tame Impala", 1.0), ("Tame Impala", 1.0), ("Adele", 0.3), ("Adele", 0.3),
        ("Tame Impala", 1.0), ("Adele", 0.3),
    ]  # fmt: skip
