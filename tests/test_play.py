"""play: queue building (order, mixing, cap, dedupe, shuffle), the watch link, by-artist songs."""

from __future__ import annotations

import random
import sqlite3

import pytest
from support.playlist import PLAYLIST, seed_playlist

from better_yt_playlist import db
from better_yt_playlist.match import set_manual
from better_yt_playlist.play import (
    WATCH_VIDEOS_MAX,
    QueueEntry,
    Reason,
    build_queue,
    from_similar,
    watch_url,
)
from better_yt_playlist.similar import Basis, Recommendation, songs_by_artist


def _e(video_id: str, reason: Reason = Reason.TRACK) -> QueueEntry:
    return QueueEntry(video_id, "artist", "track", reason)


def _ids(queue: list[QueueEntry]) -> list[str]:
    return [e.video_id for e in queue]


SEED = [_e("seed", Reason.SEED)]


def test_seed_first_then_similar_in_order() -> None:
    queue = build_queue(SEED, [_e("a"), _e("b"), _e("c")], limit=10)
    assert _ids(queue) == ["seed", "a", "b", "c"]


def test_limit_counts_the_seed_and_trims_the_weakest_similar() -> None:
    queue = build_queue(SEED, [_e("a"), _e("b"), _e("c")], limit=3)
    assert _ids(queue) == ["seed", "a", "b"]


def test_new_songs_are_spread_evenly_and_all_kept() -> None:
    main = [_e(f"m{i}") for i in range(6)]
    new = [_e("n0", Reason.NEW), _e("n1", Reason.NEW)]
    queue = build_queue(SEED, main, new, limit=9)
    assert _ids(queue) == ["seed", "m0", "m1", "n0", "m2", "m3", "n1", "m4", "m5"]


def test_new_songs_win_over_similar_ones_when_the_queue_is_full() -> None:
    main = [_e(f"m{i}") for i in range(10)]
    new = [_e("n0", Reason.NEW), _e("n1", Reason.NEW)]
    queue = build_queue(SEED, main, new, limit=5)
    assert sorted(_ids(queue)) == ["m0", "m1", "n0", "n1", "seed"]


def test_only_new_songs() -> None:
    queue = build_queue(SEED, [], [_e("n0", Reason.NEW), _e("n1", Reason.NEW)], limit=10)
    assert _ids(queue) == ["seed", "n0", "n1"]


def test_each_video_plays_once() -> None:
    queue = build_queue(SEED, [_e("a"), _e("seed"), _e("a"), _e("b")], [_e("b")], limit=10)
    assert _ids(queue) == ["seed", "a", "b"]


def test_never_longer_than_youtube_plays() -> None:
    main = [_e(f"m{i}") for i in range(100)]
    assert len(build_queue(SEED, main, limit=500)) == WATCH_VIDEOS_MAX


def test_shuffle_keeps_the_seed_first_and_the_same_songs() -> None:
    main = [_e(f"m{i}") for i in range(20)]
    queue = build_queue(SEED, main, limit=11, shuffle=True, rng=random.Random(1))
    assert queue[0].video_id == "seed"
    # Shuffles the best 10, rather than sampling 10 of the 20.
    assert sorted(_ids(queue[1:])) == sorted(f"m{i}" for i in range(10))
    assert _ids(queue[1:]) != [f"m{i}" for i in range(10)]


def test_from_similar_tags_by_basis_and_skips_songs_without_a_video() -> None:
    recs = [
        Recommendation("A", "x", 0.9, None, "v1", "t"),
        Recommendation("B", "y", 0.5, None, "v2", "t", Basis.ARTIST),
        Recommendation("C", "z", 0.4, None, None, None),
    ]
    assert [(e.video_id, e.reason) for e in from_similar(recs)] == [
        ("v1", Reason.TRACK),
        ("v2", Reason.ARTIST),
    ]


def test_watch_url_joins_ids_in_order() -> None:
    assert watch_url(["a", "b"]) == "https://www.youtube.com/watch_videos?video_ids=a,b"


@pytest.mark.parametrize("count", [0, WATCH_VIDEOS_MAX + 1])
def test_watch_url_rejects_what_youtube_would_drop(count: int) -> None:
    with pytest.raises(ValueError):
        watch_url([f"v{i}" for i in range(count)])


@pytest.fixture
def conn(tmp_path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "p.db"))
    conn = db.connect()
    seed_playlist(
        conn,
        [
            ("vLoser", "Tame Impala - Loser", None),
            ("vFeat", "Dua Lipa - Hallucinate (feat. Tame Impala)", None),
            ("vHello", "Adele - Hello", None),
            ("vLost", "Tame Impala - Lost In Yesterday", None),
            ("vRaw", "Tame Impala - The Less I Know The Better", None),  # never matched
        ],
    )
    for vid, artist, track in [
        ("vLoser", "Tame Impala", "Loser"),
        ("vFeat", "Dua Lipa, Tame Impala", "Hallucinate"),
        ("vHello", "Adele", "Hello"),
        ("vLost", "Tame Impala", "Lost in Yesterday"),
    ]:
        set_manual(conn, vid, artist, track)
    return conn


def test_songs_by_artist_in_playlist_order_skipping_unmatched(conn: sqlite3.Connection) -> None:
    songs = songs_by_artist(conn, PLAYLIST, "TAME IMPALA")
    assert [s.video_id for s in songs] == ["vLoser", "vLost"]


def test_songs_by_artist_finds_a_multi_artist_credit_by_its_first_name(
    conn: sqlite3.Connection,
) -> None:
    assert [s.video_id for s in songs_by_artist(conn, PLAYLIST, "dua lipa")] == ["vFeat"]


def test_songs_by_artist_unknown_artist(conn: sqlite3.Connection) -> None:
    assert songs_by_artist(conn, PLAYLIST, "Nirvana") == []
