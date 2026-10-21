"""warm: pre-fill discover's caches for every song, within a search budget."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from support.fakes import FakeLastfm, FakeResolver
from support.playlist import PLAYLIST, seed_playlist

from better_yt_playlist import db
from better_yt_playlist.lastfm import SimilarTrack
from better_yt_playlist.match import set_manual
from better_yt_playlist.play import DEFAULT_NEW
from better_yt_playlist.similar import (
    YoutubeHit,
    YoutubeLookupFailed,
    discover,
    resolve_seed,
)
from better_yt_playlist.warm import MAX_FAILED_SEARCHES, warm

NOW = datetime(2026, 10, 1, tzinfo=UTC)
NEW = [f"Band {i}" for i in range(MAX_FAILED_SEARCHES + 5)]  # none in the playlist


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
    return FakeLastfm(similar={("tame impala", "loser"): similar, ("adele", "hello"): similar[1:]})


def _hits() -> dict[str, YoutubeHit]:
    hits = {f"{n} - Song": YoutubeHit(f"v{n}", "t", "c") for n in NEW}
    hits["Adele - Skyfall"] = YoutubeHit("vSky", "t", "c")
    return hits


def test_after_warm_a_tap_needs_no_search(conn: sqlite3.Connection) -> None:
    api = _api()
    stats = warm(conn, PLAYLIST, api=api, resolver=FakeResolver(_hits()), max_searches=100)
    assert (stats.songs, stats.warmed, stats.stopped) == (2, 4, None)

    for new_artists in (False, True):
        tap = FakeResolver()  # finds nothing: any search would show in the results
        seed = resolve_seed(conn, PLAYLIST, "vLoser", api=api)
        recs = discover(
            conn,
            PLAYLIST,
            seed,
            api=api,
            resolver=tap,
            limit=DEFAULT_NEW,
            new_artists=new_artists,
            now=NOW,
        )
        assert (len(recs), tap.calls) == (DEFAULT_NEW, [])


def test_a_spent_budget_stops_the_run_and_the_next_run_continues(
    conn: sqlite3.Connection,
) -> None:
    api, resolver = _api(), FakeResolver(_hits())
    first = warm(conn, PLAYLIST, api=api, resolver=resolver, max_searches=3, now=NOW)
    assert first.stopped is not None and "budget" in first.stopped
    assert first.searches == len(resolver.calls) == 3

    second = warm(conn, PLAYLIST, api=api, resolver=resolver, max_searches=100, now=NOW)
    assert second.stopped is None
    assert len(resolver.calls) == len(set(resolver.calls))  # nothing searched twice


class _Failing(FakeResolver):
    def resolve(self, artist: str, track: str) -> YoutubeHit | None:
        self.calls.append(f"{artist} - {track}")
        raise YoutubeLookupFailed("HTTP Error 429")


def test_failed_searches_are_not_cached_and_stop_the_run(conn: sqlite3.Connection) -> None:
    stats = warm(conn, PLAYLIST, api=_api(), resolver=_Failing(), max_searches=100, now=NOW)
    assert stats.stopped is not None and "failed" in stats.stopped
    assert stats.failed == MAX_FAILED_SEARCHES
    assert conn.execute("SELECT COUNT(*) FROM youtube_lookups").fetchone()[0] == 0
