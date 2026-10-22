"""similar / discover: seed resolution, playlist join, exclusion, and caching."""

from __future__ import annotations

import sqlite3
import subprocess
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
    YoutubeLookupFailed,
    YtDlpResolver,
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


@pytest.mark.parametrize("query", ["yesterday tame", "lost in yesterdy", "TAME impala lost"])
def test_resolve_seed_is_forgiving(conn: sqlite3.Connection, query: str) -> None:
    assert resolve_seed(conn, PLAYLIST, query, api=FakeLastfm()).video_id == "vLost"


def test_resolve_seed_takes_a_clear_winner_over_weaker_matches(
    conn: sqlite3.Connection,
) -> None:
    seed_playlist(conn, [("vRollin", "Limp Bizkit - Rollin", None)], playlist_id=PLAYLIST)
    set_manual(conn, "vRollin", "Limp Bizkit", "Rollin")
    # "rollin" is a whole word of vRollin but only a prefix in "Rolling in the Deep".
    assert resolve_seed(conn, PLAYLIST, "rollin", api=FakeLastfm()).video_id == "vRollin"


def test_resolve_seed_ambiguous_only_when_different_songs_tie_at_the_top(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(AmbiguousSeed) as exc:
        resolve_seed(conn, PLAYLIST, "tame impala", api=FakeLastfm())
    assert [vid for vid, _ in exc.value.candidates] == ["vLoser", "vLost"]


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


def test_discover_takes_more_top_tracks_from_the_seeds_own_artist(
    conn: sqlite3.Connection,
) -> None:
    def top(artist: str, *tracks: str) -> list[TrackInfo]:
        return [TrackInfo(artist, t, None, None) for t in tracks]

    api = FakeLastfm(
        known=[LOSER],
        similar_artist_map={"tame impala": [SimilarArtist("MGMT", 0.9)]},
        top={
            # Loser is the seed and Lost in Yesterday is in the playlist: both skipped.
            "tame impala": top(
                "Tame Impala",
                "Loser",
                "Lost in Yesterday",
                "Borderline",
                "Elephant",
                "Let It Happen",
                "The Less I Know the Better",
            ),
            "mgmt": top("MGMT", "Electric Feel", "Kids"),
        },
    )
    hits = ["Borderline", "Elephant", "Let It Happen", "The Less I Know the Better"]
    resolver = FakeResolver(
        hits={f"Tame Impala - {t}": YoutubeHit(f"v{t}", "t", "c") for t in hits}
        | {"MGMT - Electric Feel": YoutubeHit("vMGMT", "t", "c")}
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    recs = discover(conn, PLAYLIST, seed, api=api, resolver=resolver, limit=10, now=NOW)
    assert [r.video_id for r in recs] == ["vBorderline", "vElephant", "vLet It Happen", "vMGMT"]
    # Only as many searches as songs taken.
    assert len(resolver.calls) == 4


def test_discover_seed_artist_quota_never_searches_past_the_limit(
    conn: sqlite3.Connection,
) -> None:
    api = FakeLastfm(
        known=[LOSER],
        similar_artist_map={"tame impala": [SimilarArtist("MGMT", 0.9)]},
        top={
            "tame impala": [TrackInfo("Tame Impala", t, None, None) for t in "ABC"],
            "mgmt": [TrackInfo("MGMT", "Kids", None, None)],
        },
    )
    resolver = FakeResolver(
        hits={f"Tame Impala - {t}": YoutubeHit(f"v{t}", "t", "c") for t in "ABC"}
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    recs = discover(conn, PLAYLIST, seed, api=api, resolver=resolver, limit=2, now=NOW)
    assert [r.video_id for r in recs] == ["vA", "vB"]
    assert resolver.calls == ["Tame Impala - A", "Tame Impala - B"]


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


def test_discover_new_artists_skips_every_artist_the_playlist_has(
    conn: sqlite3.Connection,
) -> None:
    api = FakeLastfm(
        known=[LOSER],
        similar={
            ("tame impala", "loser"): [
                _sim("Adele", "Skyfall", 0.9),  # new song, known artist
                _sim("MGMT", "Electric Feel", 0.8),
                _sim("Adele & MGMT", "Duet", 0.7),  # credits a known artist first
            ]
        },
        similar_artist_map={
            "tame impala": [SimilarArtist("Adele", 0.6), SimilarArtist("Pond", 0.5)]
        },
        top={
            "tame impala": [TrackInfo("Tame Impala", "Elephant", None, None)],
            "adele": [TrackInfo("Adele", "Easy on Me", None, None)],
            "pond": [TrackInfo("Pond", "Sitting Up on Our Crane", None, None)],
        },
    )
    resolver = FakeResolver(
        hits={
            "Adele - Skyfall": YoutubeHit("vSky", "t", "c"),
            "MGMT - Electric Feel": YoutubeHit("vMGMT", "t", "c"),
            "Adele & MGMT - Duet": YoutubeHit("vDuet", "t", "c"),
            "Tame Impala - Elephant": YoutubeHit("vEle", "t", "c"),
            "Adele - Easy on Me": YoutubeHit("vEasy", "t", "c"),
            "Pond - Sitting Up on Our Crane": YoutubeHit("vPond", "t", "c"),
        }
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)

    everything = discover(conn, PLAYLIST, seed, api=api, resolver=resolver, limit=10, now=NOW)
    assert [r.video_id for r in everything] == ["vSky", "vMGMT", "vDuet", "vEle", "vEasy", "vPond"]

    fresh = discover(
        conn, PLAYLIST, seed, api=api, resolver=resolver, limit=10, new_artists=True, now=NOW
    )
    assert [r.video_id for r in fresh] == ["vMGMT", "vPond"]


def test_discover_runs_its_youtube_searches_in_parallel(conn: sqlite3.Connection) -> None:
    import threading

    names = ["A", "B", "C"]
    api = _api_with_similar(*(_sim(n, "Song", 0.5) for n in names))
    # Each search waits for all three to be in flight: one-by-one would time out.
    barrier = threading.Barrier(len(names), timeout=5)

    class Concurrent(FakeResolver):
        def resolve(self, artist: str, track: str) -> YoutubeHit | None:
            barrier.wait()
            return FakeResolver.resolve(self, artist, track)

    resolver = Concurrent(hits={f"{n} - Song": YoutubeHit(f"v{n}", "t", "c") for n in names})
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    recs = discover(conn, PLAYLIST, seed, api=api, resolver=resolver, limit=3, now=NOW)
    assert [r.video_id for r in recs] == ["vA", "vB", "vC"]  # still ranked


def test_discover_batches_never_search_more_than_needed(conn: sqlite3.Connection) -> None:
    api = _api_with_similar(*(_sim(n, "Song", 0.5) for n in "ABCDEF"))
    resolver = FakeResolver(
        hits={f"{n} - Song": YoutubeHit(f"v{n}", "t", "c") for n in "ACDEF"}  # B: not found
    )
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    recs = discover(conn, PLAYLIST, seed, api=api, resolver=resolver, limit=3, now=NOW)
    assert [r.video_id for r in recs] == ["vA", "vC", "vD"]
    # A, B, C in the first batch; one still missing, so only D next. E, F untouched.
    assert sorted(resolver.calls) == ["A - Song", "B - Song", "C - Song", "D - Song"]


def test_a_failed_search_is_skipped_and_not_cached(conn: sqlite3.Connection) -> None:
    api = _api_with_similar(_sim("A", "Song", 0.9), _sim("B", "Song", 0.8))

    class FailsOnA(FakeResolver):
        def resolve(self, artist: str, track: str) -> YoutubeHit | None:
            if artist == "A":
                raise YoutubeLookupFailed("HTTP Error 429")
            return FakeResolver.resolve(self, artist, track)

    resolver = FailsOnA(hits={"B - Song": YoutubeHit("vB", "t", "c")})
    seed = resolve_seed(conn, PLAYLIST, "loser", api=api)
    recs = discover(conn, PLAYLIST, seed, api=api, resolver=resolver, limit=5, now=NOW)
    assert [r.video_id for r in recs] == ["vB"]
    assert resolver.calls == ["B - Song"]  # A tried once (in the batch), not again
    cached = {r["artist"] for r in conn.execute("SELECT artist FROM youtube_lookups")}
    assert cached == {"b"}  # A is retried next time


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [(0, "", None), (0, "vX\tX - Song\tX\n", YoutubeHit("vX", "X - Song", "X"))],
)
def test_ytdlp_resolver_no_results_is_none(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, expected: YoutubeHit | None
) -> None:
    done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    assert YtDlpResolver().resolve("X", "Song") == expected


def test_ytdlp_resolver_raises_when_yt_dlp_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    done = subprocess.CompletedProcess([], 1, stdout="", stderr="ERROR: HTTP Error 429\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    with pytest.raises(YoutubeLookupFailed, match="429"):
        YtDlpResolver().resolve("X", "Song")
