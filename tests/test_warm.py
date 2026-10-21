"""warm: build every song's page queue ahead of time — paced, time-boxed, resumable."""

from __future__ import annotations

import sqlite3
import threading

import pytest
from support.fakes import FakeLastfm, FakeResolver
from support.playlist import PLAYLIST, seed_playlist

from better_yt_playlist import db
from better_yt_playlist import warm as warm_module
from better_yt_playlist.lastfm import SimilarArtist, SimilarTrack, TrackInfo
from better_yt_playlist.match import set_manual
from better_yt_playlist.play import DEFAULT_LIMIT, DEFAULT_NEW, Reason, seed_queue
from better_yt_playlist.similar import YoutubeHit, YoutubeLookupFailed, resolve_seed
from better_yt_playlist.warm import MAX_FAILED_IN_A_ROW, WarmStats, warm

NEW = [f"Band {i}" for i in range(2 * MAX_FAILED_IN_A_ROW)]  # none in the playlist


@pytest.fixture
def conn(tmp_path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "w.db"))
    conn = db.connect()
    seed_playlist(
        conn,
        [
            ("vLoser", "Tame Impala - Loser", None),
            ("vLoser2", "Tame Impala - Loser (Live)", None),  # same song: warmed once
            ("vHello", "Adele - Hello", None),
        ],
    )
    for vid, artist, track in [
        ("vLoser", "Tame Impala", "Loser"),
        ("vLoser2", "Tame Impala", "Loser"),
        ("vHello", "Adele", "Hello"),
    ]:
        set_manual(conn, vid, artist, track)
    return conn


def _api() -> FakeLastfm:
    similar = [
        SimilarTrack("Adele", "Skyfall", 0.9, None, None),  # known artist: skipped by new_artists
        *(SimilarTrack(n, "Song", 0.5, None, None) for n in NEW),
    ]
    return FakeLastfm(
        similar={("tame impala", "loser"): similar, ("adele", "hello"): similar[1:]},
        # A short playlist sends `similar` into its artist fill: Last.fm calls of its own.
        similar_artist_map={"tame impala": [SimilarArtist("Adele", 0.5)]},
        top={"adele": [TrackInfo("Adele", "Easy on Me", None, None)]},
    )


def _hits() -> dict[str, YoutubeHit]:
    hits = {f"{n} - Song": YoutubeHit(f"v{n}", "t", "c") for n in NEW}
    hits["Adele - Skyfall"] = YoutubeHit("vSky", "t", "c")
    return hits


class _Clock:
    """A clock that only moves when warm sleeps."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _warm(
    conn: sqlite3.Connection, api: FakeLastfm, resolver: FakeResolver, **kw: float
) -> WarmStats:
    clock = _Clock()
    opts = {"max_minutes": 60.0, "pause_s": 1.0} | kw
    return warm(conn, PLAYLIST, api=api, resolver=resolver, sleep=clock.sleep, clock=clock, **opts)


def test_after_warm_a_tap_makes_no_lastfm_call_or_search(conn: sqlite3.Connection) -> None:
    api = _api()
    stats = _warm(conn, api, FakeResolver(_hits()))
    assert (stats.songs, stats.warmed, stats.stopped) == (2, 4, None)

    calls_after_warm = (api.similar_calls, api.artist_calls)
    seed = resolve_seed(conn, PLAYLIST, "vLoser", api=api)
    for new_artists in (False, True):
        tap = FakeResolver()  # finds nothing: any search would show in the queue
        queue = seed_queue(  # as the page builds it
            conn,
            PLAYLIST,
            seed,
            api=api,
            limit=DEFAULT_LIMIT,
            new=DEFAULT_NEW,
            new_artists=new_artists,
            resolver=tap,
        )
        assert sum(e.reason is Reason.NEW for e in queue) == DEFAULT_NEW
        assert tap.calls == []
    assert (api.similar_calls, api.artist_calls) == calls_after_warm


def test_the_time_limit_stops_the_run_and_the_next_run_continues(
    conn: sqlite3.Connection,
) -> None:
    api, resolver = _api(), FakeResolver(_hits())
    first = _warm(conn, api, resolver, max_minutes=3, pause_s=60)  # room for 3 searches
    assert first.stopped == "time limit reached"
    assert first.searches == len(resolver.calls) == 3

    second = _warm(conn, api, resolver)
    assert second.stopped is None
    assert len(resolver.calls) == len(set(resolver.calls))  # nothing searched twice


class _Failing(FakeResolver):
    """Fails every ``every``-th search (every one by default), finds the rest."""

    def __init__(self, hits: dict[str, YoutubeHit], every: int = 1) -> None:
        super().__init__(hits)
        self.every = every

    def resolve(self, artist: str, track: str) -> YoutubeHit | None:
        hit = FakeResolver.resolve(self, artist, track)
        if len(self.calls) % self.every == 0:
            raise YoutubeLookupFailed("HTTP Error 429")
        return hit


def test_failures_in_a_row_stop_the_run_and_are_not_cached(conn: sqlite3.Connection) -> None:
    stats = _warm(conn, _api(), _Failing(_hits()))
    assert stats.stopped is not None and "in a row" in stats.stopped
    assert stats.failed == MAX_FAILED_IN_A_ROW
    assert conn.execute("SELECT COUNT(*) FROM youtube_lookups").fetchone()[0] == 0


def test_scattered_failures_dont_stop_the_run(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(warm_module, "MAX_FAILED_IN_A_ROW", 3)
    stats = _warm(conn, _api(), _Failing(_hits(), every=2))  # fail, find, fail, ...
    assert stats.stopped is None
    assert stats.failed > 3  # more in total than the in-a-row limit


def test_searches_run_one_at_a_time(conn: sqlite3.Connection) -> None:
    class Overlap(FakeResolver):
        def __init__(self, hits: dict[str, YoutubeHit]) -> None:
            super().__init__(hits)
            self.lock, self.active, self.most = threading.Lock(), 0, 0

        def resolve(self, artist: str, track: str) -> YoutubeHit | None:
            with self.lock:
                self.active += 1
                self.most = max(self.most, self.active)
            threading.Event().wait(0.01)  # long enough for a parallel search to overlap
            with self.lock:
                self.active -= 1
            return FakeResolver.resolve(self, artist, track)

    resolver = Overlap(_hits())
    _warm(conn, _api(), resolver)
    assert len(resolver.calls) > 1
    assert resolver.most == 1
