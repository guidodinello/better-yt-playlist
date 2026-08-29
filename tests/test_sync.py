"""Sync: duration parsing, pagination, left-join enrichment, soft delete."""

from __future__ import annotations

import json

from better_yt_playlist import db
from better_yt_playlist.db import Quota
from better_yt_playlist.sync import sync
from better_yt_playlist.youtube import parse_duration, parse_playlist_id


def test_parse_duration() -> None:
    assert parse_duration("PT4M13S") == 253
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("PT0S") == 0
    assert parse_duration("P1DT2H") == 93600
    assert parse_duration(None) is None
    assert parse_duration("garbage") is None


def test_parse_playlist_id() -> None:
    assert parse_playlist_id("PLabc123") == "PLabc123"
    assert parse_playlist_id("https://www.youtube.com/watch?v=x&list=PLabc123") == "PLabc123"
    assert parse_playlist_id("https://www.youtube.com/playlist?list=PLxyz") == "PLxyz"


class _Req:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def execute(self) -> dict:
        return self.payload


class _PlaylistItems:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def list(self, **_kw):  # noqa: A003
        return _Req({**self._pages[0], "_page": 0})

    def list_next(self, _request, response):
        nxt = response["_page"] + 1
        if nxt >= len(self._pages):
            return None
        return _Req({**self._pages[nxt], "_page": nxt})


class _Videos:
    def __init__(self, videos: dict) -> None:
        self._videos = videos

    def list(self, *, id, **_kw):  # noqa: A003
        items = [self._videos[i] for i in id.split(",") if i in self._videos]
        return _Req({"items": items})


class FakeClient:
    def __init__(self, pages: list[dict], videos: dict) -> None:
        self._pi = _PlaylistItems(pages)
        self._v = _Videos(videos)

    def playlistItems(self):  # noqa: N802
        return self._pi

    def videos(self):
        return self._v


def _pi(item_id: str, video_id: str, position: int, *, owner: str | None = "Chan") -> dict:
    snippet = {
        "title": f"title-{video_id}",
        "position": position,
        "resourceId": {"videoId": video_id},
    }
    if owner is not None:
        snippet["videoOwnerChannelTitle"] = owner
        snippet["videoOwnerChannelId"] = "UC" + owner
    return {"id": item_id, "snippet": snippet, "contentDetails": {"videoId": video_id}}


def _video(video_id: str, seconds: int = 60) -> dict:
    return {
        "id": video_id,
        "snippet": {
            "description": f"desc-{video_id}",
            "publishedAt": "2020-01-01T00:00:00Z",
            "tags": ["a", "b"],
        },
        "contentDetails": {"duration": f"PT{seconds}S"},
        "statistics": {"viewCount": "1234"},
    }


def test_pagination_left_join_and_soft_delete(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "t.db"))

    pages = [
        {"items": [_pi("a", "v1", 0), _pi("b", "v2", 1)]},
        {"items": [_pi("c", "vdead", 2, owner=None)]},
    ]
    videos = {"v1": _video("v1", 30), "v2": _video("v2", 90)}

    conn = db.connect()
    stats = sync("PL1", conn=conn, client=FakeClient(pages, videos))
    assert stats["items"] == 3
    assert stats["dead_entries"] == 1

    rows = {r["playlist_item_id"]: r for r in conn.execute("SELECT * FROM playlist_items")}
    assert rows["a"]["duration_s"] == 30
    assert json.loads(rows["a"]["tags"]) == ["a", "b"]
    assert rows["a"]["view_count"] == 1234
    assert rows["c"]["channel_title"] is None  # dead entry: left join misses
    assert rows["c"]["duration_s"] is None

    pages2 = [{"items": [_pi("a", "v1", 0), _pi("c", "vdead", 1, owner=None)]}]
    sync("PL1", conn=conn, client=FakeClient(pages2, videos))
    b = conn.execute("SELECT removed_at FROM playlist_items WHERE playlist_item_id='b'").fetchone()
    assert b["removed_at"] is not None
    a = conn.execute("SELECT removed_at FROM playlist_items WHERE playlist_item_id='a'").fetchone()
    assert a["removed_at"] is None


def test_quota_used_today_sums_recent_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "q.db"))
    conn = db.connect()
    conn.execute(
        "INSERT INTO quota_log (ts, units, method) VALUES ('2000-01-01T00:00:00Z', 999, 'old')"
    )
    conn.commit()
    q = Quota(conn)
    q.charge(5, "playlistItems.list")
    assert q.used_today() == 5
