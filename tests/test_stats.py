"""stats: facts about the live entries of a playlist, with watched counts when history exists."""

from __future__ import annotations

from typing import Any

from better_yt_playlist import db
from better_yt_playlist.stats import playlist_stats, render

PL = "PLx"


def _add(conn: Any, item: str, vid: str, channel: str, secs: int, **extra: Any) -> None:
    row = {
        "playlist_item_id": item,
        "playlist_id": PL,
        "video_id": vid,
        "position": 0,
        "title": f"title-{vid}",
        "channel_title": channel,
        "channel_id": f"UC{channel}",
        "duration_s": secs,
        "published_at": "2020-01-01T00:00:00Z",
        "view_count": 100,
        "added_at": "2026-10-01T00:00:00Z",
        "synced_at": "2026-11-19T00:00:00Z",
        "removed_at": None,
        **extra,
    }
    cols = ", ".join(row)
    conn.execute(
        f"INSERT INTO playlist_items ({cols}) VALUES ({', '.join(':' + c for c in row)})", row
    )


def _db(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "s.db"))
    conn = db.connect()
    _add(conn, "a1", "va1", "Alpha", 3600, view_count=5_000_000)
    _add(conn, "a2", "va2", "Alpha", 600, published_at="2010-02-08T00:00:00Z")
    _add(conn, "b1", "vb1", "Beta", 30, view_count=7)
    _add(conn, "gone", "vg", "Gamma", 99_999, removed_at="2026-11-19T00:00:00Z")
    _add(conn, "other", "vo", "Alpha", 60, playlist_id="PLother")
    conn.commit()
    return conn


def test_counts_only_live_entries_of_the_playlist(tmp_path: Any, monkeypatch: Any) -> None:
    s = playlist_stats(_db(tmp_path, monkeypatch), PL)
    assert s["videos"] == 3
    assert s["hours"] == round(4230 / 3600, 1)
    assert s["channels"] == 2
    assert s["one_video_channels"] == 1
    assert s["buckets"] == {"<=1m": 1, "1-10m": 0, "10-30m": 1, "30-60m": 0, "1h+": 1}
    assert [c["channel_title"] for c in s["top_by_videos"]] == ["Alpha", "Beta"]
    assert s["longest"][0]["title"] == "title-va1"  # the removed 27 h video doesn't count
    assert s["oldest"][0]["published_at"].startswith("2010")
    assert s["most_viewed"][0]["view_count"] == 5_000_000
    assert s["least_viewed"][0]["view_count"] == 7
    assert s["has_history"] is False


def test_watched_counts_come_from_watch_history(tmp_path: Any, monkeypatch: Any) -> None:
    conn = _db(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO watch_history (video_id, position, first_seen_at, last_seen_at) "
        "VALUES ('va1', 0, 'x', 'x')"
    )
    conn.commit()
    s = playlist_stats(conn, PL)
    assert s["has_history"] is True
    assert s["watched"] == 1
    assert s["watched_hours"] == 1.0
    alpha = next(c for c in s["top_by_videos"] if c["channel_title"] == "Alpha")
    assert alpha["watched"] == 1
    assert "1 watched (50%)" in render(s)


def test_empty_playlist_says_so(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "e.db"))
    assert "no live videos" in render(playlist_stats(db.connect(), "PLnone"))
