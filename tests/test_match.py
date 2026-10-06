"""match: titles → confirmed Last.fm identity, stored in track_metadata."""

from __future__ import annotations

import sqlite3

import pytest
from support.fakes import FakeLastfm
from support.playlist import PLAYLIST, seed_playlist

from better_yt_playlist import db
from better_yt_playlist.lastfm import LastfmError, TrackInfo
from better_yt_playlist.match import identify, match_playlist, set_manual


@pytest.fixture
def conn(tmp_path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "m.db"))
    return db.connect()


def _meta(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["video_id"]: r for r in conn.execute("SELECT * FROM track_metadata")}


def test_parsed_title_confirmed_and_canonicalized() -> None:
    loser = TrackInfo("Tame Impala", "Loser", None, "u", listeners=674_666)
    api = FakeLastfm(known=[loser])
    result = identify(api, "tame impala - LOSER (Official Video)", "tameimpalaVEVO")
    assert result.info == loser
    assert result.source == "parsed"


def test_track_first_title_beats_a_junk_scrobble_entry() -> None:
    # Real case: Last.fm has a 27-listener entry for artist "Loser".
    junk = TrackInfo(
        "Loser", "Tame Impala (Spider-Man: Brand New Day Soundtrack)", None, None, listeners=27
    )
    real = TrackInfo("Tame Impala", "Loser", None, None, listeners=674_666)
    api = FakeLastfm(known=[junk, real])
    result = identify(api, "Loser - Tame Impala (Spider-Man: Brand New Day Soundtrack)", "x")
    assert result.info == real


def test_niche_song_still_matches_when_nothing_bigger_exists() -> None:
    niche = TrackInfo("Small Band", "Local Song", None, None, listeners=40)
    api = FakeLastfm(known=[niche])
    assert identify(api, "Small Band - Local Song", "x").info == niche


def test_multi_artist_credit_falls_back_to_first_artist() -> None:
    api = FakeLastfm(known=[TrackInfo("Bad Bunny", "No Te Hagas", None, None, listeners=90_000)])
    result = identify(api, "Bad Bunny x Jory Boy - No Te Hagas (Video Oficial)", "x")
    assert result.info is not None
    assert result.info.artist == "Bad Bunny"
    assert api.info_calls == [("Bad Bunny x Jory Boy", "No Te Hagas"), ("Bad Bunny", "No Te Hagas")]


def test_unparseable_title_uses_most_listened_search_hit() -> None:
    # Shape of the real track.search response for this title (2026-10-05).
    junk = TrackInfo("pelomusicgroup", "MIRANDA! Fantasmas", None, None, listeners=26)
    real = TrackInfo("Miranda!", "Fantasmas", None, None, listeners=35_394)
    acoustic = TrackInfo("Miranda!", "Fantasmas - Versión acústica", None, None, listeners=2_010)
    api = FakeLastfm(
        known=[junk, real, acoustic],
        search_results={"MIRANDA! Fantasmas": [junk, real, acoustic]},
    )
    result = identify(api, "MIRANDA! Fantasmas (Video Oficial)", "Pelo Music Group")
    assert result.info == real
    assert result.source == "lastfm_search"


def test_search_hit_must_be_named_in_the_title() -> None:
    # Real wrong match before this rule: a popular *different* song.
    elton = TrackInfo("Elton John", "The Bitch Is Back", None, None, listeners=900_000)
    api = FakeLastfm(known=[elton], search_results={"The Vamp Is Back": [elton]})
    assert identify(api, "The Vamp Is Back", "Some Channel").info is None


def test_search_hits_below_listener_floor_are_not_taken() -> None:
    junk = TrackInfo("Buttonik", "GTA San Andreas Theme Song Full ! !", None, None, listeners=862)
    api = FakeLastfm(known=[junk], search_results={"GTA San Andreas Theme Song Full ! !": [junk]})
    result = identify(api, "GTA San Andreas Theme Song Full ! !", "Buttonik")
    assert result.info is None


def test_match_playlist_stores_matches_and_not_found(conn: sqlite3.Connection) -> None:
    seed_playlist(
        conn,
        [
            ("v1", "Adele - Rolling in the Deep (Official Music Video)", "Adele"),
            ("v2", "GTA San Andreas Theme Song Full ! !", "Buttonik"),
            ("v1", "Adele - Rolling in the Deep (Official Music Video)", "Adele"),  # duplicate
        ],
    )
    api = FakeLastfm(known=[TrackInfo("Adele", "Rolling in the Deep", None, None)])

    stats = match_playlist(PLAYLIST, conn=conn, api=api)

    assert (stats.matched, stats.not_found) == (1, 1)
    meta = _meta(conn)
    assert (meta["v1"]["artist"], meta["v1"]["status"]) == ("Adele", "matched")
    assert meta["v2"]["status"] == "not_found"


def test_rerun_skips_known_and_retry_revisits_not_found(conn: sqlite3.Connection) -> None:
    seed_playlist(conn, [("v1", "Some Obscure Thing", None)])
    match_playlist(PLAYLIST, conn=conn, api=FakeLastfm())

    thing = TrackInfo("X", "Thing", None, None, listeners=5_000)
    api = FakeLastfm(search_results={"Some Obscure Thing": [thing]}, known=[thing])
    assert match_playlist(PLAYLIST, conn=conn, api=api).matched == 0
    assert match_playlist(PLAYLIST, conn=conn, api=api, retry=True).matched == 1
    assert _meta(conn)["v1"]["artist"] == "X"


def test_manual_rows_are_never_overwritten(conn: sqlite3.Connection) -> None:
    seed_playlist(conn, [("v1", "Adele - Hello", "Adele")])
    set_manual(conn, "v1", "Adele", "Hello (Live)")
    api = FakeLastfm(known=[TrackInfo("Adele", "Hello", None, None)])

    match_playlist(PLAYLIST, conn=conn, api=api, retry=True)

    row = _meta(conn)["v1"]
    assert (row["track"], row["status"]) == ("Hello (Live)", "manual")


def test_set_manual_rejects_unknown_video(conn: sqlite3.Connection) -> None:
    with pytest.raises(SystemExit, match="no video"):
        set_manual(conn, "nope", "A", "B")


def test_progress_is_kept_when_lastfm_fails_mid_run(conn: sqlite3.Connection) -> None:
    class FailsOnSecond(FakeLastfm):
        def track_info(self, artist: str, track: str) -> TrackInfo | None:
            if artist == "Boom":
                raise LastfmError("network error")
            return super().track_info(artist, track)

    seed_playlist(conn, [("v1", "Adele - Hello", "Adele"), ("v2", "Boom - Bang", "x")])
    api = FailsOnSecond(known=[TrackInfo("Adele", "Hello", None, None, listeners=10_000)])
    with pytest.raises(LastfmError):
        match_playlist(PLAYLIST, conn=conn, api=api)
    assert set(_meta(conn)) == {"v1"}


def test_dead_and_removed_videos_are_skipped(conn: sqlite3.Connection) -> None:
    seed_playlist(conn, [("dead", "Deleted video", None), ("gone", "Adele - Hello", "Adele")])
    conn.execute("UPDATE playlist_items SET unavailable_at = 'x' WHERE video_id = 'dead'")
    conn.execute("UPDATE playlist_items SET removed_at = 'x' WHERE video_id = 'gone'")
    stats = match_playlist(PLAYLIST, conn=conn, api=FakeLastfm())
    assert (stats.matched, stats.not_found) == (0, 0)
