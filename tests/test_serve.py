"""serve: the phone page — search, the queue page and its play link, and error pages."""

from __future__ import annotations

import sqlite3
from html import unescape

import pytest
from fastapi.testclient import TestClient
from support.fakes import FakeLastfm, FakeResolver
from support.playlist import PLAYLIST, seed_playlist

from better_yt_playlist import db
from better_yt_playlist.db import connect as connect_in_thread
from better_yt_playlist.lastfm import LastfmError, SimilarTrack
from better_yt_playlist.match import set_manual
from better_yt_playlist.serve import create_app
from better_yt_playlist.similar import YoutubeHit
from better_yt_playlist.titles import norm

LOSER_SIMILAR = [
    SimilarTrack("Tame Impala", "Lost in Yesterday", 0.9, None, None),
    SimilarTrack("Adele", "Hello", 0.5, None, None),
    SimilarTrack("New Band", "Fresh Song", 0.4, None, None),  # not in the playlist
]


@pytest.fixture
def conn(tmp_path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "serve.db"))
    conn = db.connect()
    seed_playlist(
        conn,
        [
            ("vLoser", "Tame Impala - Loser", None),
            ("vLost", "Tame Impala - Lost In Yesterday", None),
            ("vHello", "Adele - Hello", None),
            ("vDespecha", "ROSALÍA - DESPECHÁ", None),
            ("vRaw", 'Some <b>"raw"</b> upload', None),  # never matched
        ],
    )
    for vid, artist, track in [
        ("vLoser", "Tame Impala", "Loser"),
        ("vLost", "Tame Impala", "Lost in Yesterday"),
        ("vHello", "Adele", "Hello"),
        ("vDespecha", "ROSALÍA", "DESPECHÁ"),
    ]:
        set_manual(conn, vid, artist, track)
    return conn


def _client(lastfm: FakeLastfm | None = None, resolver: FakeResolver | None = None) -> TestClient:
    api = lastfm or FakeLastfm(similar={norm("Tame Impala", "Loser"): LOSER_SIMILAR})
    # The app opens (and closes) a connection per request in its worker threads, so it gets
    # the real connect: conftest's tracking wrapper would close them again on the main thread.
    app = create_app(PLAYLIST, connect=connect_in_thread, lastfm=lambda: api, resolver=resolver)
    return TestClient(app, raise_server_exceptions=False)


def _play_ids(html: str) -> list[str]:
    marker = "watch_videos?video_ids="
    start = html.index(marker) + len(marker)
    return unescape(html[start : html.index('"', start)]).split(",")


@pytest.mark.usefixtures("conn")
def test_search_lists_every_playable_song_in_playlist_order() -> None:
    page = _client().get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    order = [page.text.index(f"v={vid}") for vid in ("vLoser", "vLost", "vHello", "vDespecha")]
    assert order == sorted(order)
    assert "5 songs" in page.text


@pytest.mark.usefixtures("conn")
def test_search_folds_case_beyond_ascii() -> None:
    page = _client().get("/", params={"q": "rosalía"})
    assert "v=vDespecha" in page.text
    assert "v=vLoser" not in page.text


@pytest.mark.usefixtures("conn")
def test_search_is_forgiving_and_ranks_the_best_match_first() -> None:
    page = _client().get("/", params={"q": "yesterday tame impla"})  # any order, a typo
    assert "v=vLost" in page.text
    assert "v=vLoser" not in page.text  # "yesterday" must match too


@pytest.mark.usefixtures("conn")
def test_songs_are_listed_by_their_youtube_title_with_the_artist() -> None:
    page = _client().get("/", params={"q": "lost in yesterday"})
    assert "Tame Impala - Lost In Yesterday</a>" in page.text
    assert "artist=Tame+Impala" in page.text


@pytest.mark.usefixtures("conn")
def test_the_queue_shows_youtube_titles() -> None:
    page = _client().get("/play", params={"v": "vLoser"})
    assert "Tame Impala - Loser</a>" in page.text  # the seed
    assert "Tame Impala - Lost In Yesterday</a>" in page.text  # a similar song


@pytest.mark.usefixtures("conn")
def test_search_escapes_titles_and_flags_unmatched_songs() -> None:
    page = _client().get("/", params={"q": "raw"})
    assert "<b>" not in page.text
    assert "&lt;b&gt;&quot;raw&quot;" in page.text
    assert "not matched" in page.text


@pytest.mark.usefixtures("conn")
def test_search_skips_removed_and_unavailable_songs(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE playlist_items SET removed_at = 'x' WHERE video_id = 'vHello'")
    conn.execute("UPDATE playlist_items SET unavailable_at = 'x' WHERE video_id = 'vLost'")
    conn.commit()
    page = _client().get("/")
    assert "v=vHello" not in page.text
    assert "v=vLost" not in page.text


@pytest.mark.usefixtures("conn")
def test_play_a_song_links_the_seed_then_its_similar_playlist_songs() -> None:
    page = _client().get("/play", params={"v": "vLoser"})
    assert page.status_code == 200
    assert _play_ids(page.text) == ["vLoser", "vLost", "vHello"]
    assert "Play 3 songs" in page.text


@pytest.mark.usefixtures("conn")
def test_play_an_artist_links_their_songs() -> None:
    page = _client().get("/play", params={"artist": "tame impala"})
    assert _play_ids(page.text) == ["vLoser", "vLost"]


@pytest.mark.usefixtures("conn")
def test_new_songs_are_offered_and_mixed_in_only_with_yt_dlp() -> None:
    resolver = FakeResolver(hits={"New Band - Fresh Song": YoutubeHit("vFresh", "t", "c")})
    with_ytdlp = _client(resolver=resolver).get("/play", params={"v": "vLoser", "new": 1})
    assert "vFresh" in _play_ids(with_ytdlp.text)
    assert "name=new" in with_ytdlp.text

    without = _client().get("/play", params={"v": "vLoser", "new": 1})
    assert "vFresh" not in _play_ids(without.text)
    assert "name=new" not in without.text


@pytest.mark.usefixtures("conn")
def test_tapping_a_song_mixes_in_new_songs_by_default_and_0_turns_it_off() -> None:
    resolver = FakeResolver(hits={"New Band - Fresh Song": YoutubeHit("vFresh", "t", "c")})
    tapped = _client(resolver=resolver).get("/play", params={"v": "vLoser"})
    assert "vFresh" in _play_ids(tapped.text)

    off = _client(resolver=resolver).get("/play", params={"v": "vLoser", "new": 0})
    assert "vFresh" not in _play_ids(off.text)


@pytest.mark.usefixtures("conn")
def test_only_new_artists_drops_new_songs_by_playlist_artists() -> None:
    api = FakeLastfm(
        similar={
            norm("Tame Impala", "Loser"): [
                *LOSER_SIMILAR,
                SimilarTrack("Adele", "Skyfall", 0.3, None, None),  # new song, known artist
            ]
        }
    )
    resolver = FakeResolver(
        hits={
            "New Band - Fresh Song": YoutubeHit("vFresh", "t", "c"),
            "Adele - Skyfall": YoutubeHit("vSky", "t", "c"),
        }
    )
    client = _client(lastfm=api, resolver=resolver)
    assert {"vFresh", "vSky"} <= set(_play_ids(client.get("/play", params={"v": "vLoser"}).text))

    page = client.get("/play", params={"v": "vLoser", "new_artists": 1})
    assert "vFresh" in _play_ids(page.text)
    assert "vSky" not in _play_ids(page.text)
    assert "name=new_artists value=1 checked" in page.text


@pytest.mark.usefixtures("conn")
@pytest.mark.parametrize(
    "params",
    [
        {},  # neither a song nor an artist
        {"v": "vLoser", "artist": "Adele"},
        {"v": "vLoser", "limit": "0"},
        {"v": "vLoser", "limit": "lots"},
        {"artist": "Nirvana"},
    ],
)
def test_bad_requests_get_an_html_page(params: dict[str, str]) -> None:
    page = _client().get("/play", params=params)
    assert page.status_code == 400
    assert page.headers["content-type"].startswith("text/html")


@pytest.mark.usefixtures("conn")
def test_unknown_song_and_unknown_path_are_404_pages() -> None:
    for page in (_client().get("/play", params={"v": "vNope"}), _client().get("/nope")):
        assert page.status_code == 404
        assert page.headers["content-type"].startswith("text/html")


class _DownLastfm(FakeLastfm):
    def similar_tracks(self, artist: str, track: str, *, limit: int) -> list[SimilarTrack]:
        raise LastfmError("track.getSimilar failed (HTTP 503)")


@pytest.mark.usefixtures("conn")
def test_lastfm_failure_is_a_502_page() -> None:
    page = _client(lastfm=_DownLastfm()).get("/play", params={"v": "vLoser"})
    assert page.status_code == 502
    assert "Last.fm" in page.text
