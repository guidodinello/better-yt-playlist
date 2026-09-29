"""dedupe: extra copies of a video are deleted, keeping the earliest-added one."""

from __future__ import annotations

from typing import Any

from better_yt_playlist import db
from better_yt_playlist.dedupe import dedupe, find_duplicates

Json = dict[str, Any]

PL = "PLtarget"


def _item(item_id: str, vid: str, added: str, position: int) -> Json:
    return {
        "id": item_id,
        "snippet": {
            "publishedAt": added,
            "position": position,
            "resourceId": {"kind": "youtube#video", "videoId": vid},
        },
        "contentDetails": {"videoId": vid},
    }


class _Req:
    def __init__(self, payload: Json) -> None:
        self.payload = payload

    def execute(self) -> Json:
        return self.payload


class PlaylistFake:
    """Fake client serving a live playlist that ``delete`` actually shrinks."""

    def __init__(self, items: list[Json]) -> None:
        self.items = items
        self.deleted: list[str] = []

    def playlistItems(self) -> PlaylistFake:  # noqa: N802
        return self

    def list(self, **_: Any) -> _Req:  # noqa: A003
        return _Req({"items": list(self.items)})

    def list_next(self, *_: Any) -> None:
        return None

    def delete(self, *, id: str) -> _Req:  # noqa: A002
        self.deleted.append(id)
        self.items = [it for it in self.items if it["id"] != id]
        return _Req({})


def test_find_duplicates_keeps_earliest_added_copy_of_each_video() -> None:
    items = [
        _item("a-late", "a", "2026-09-04T00:00:00Z", 0),
        _item("a-first", "a", "2026-08-29T00:00:00Z", 1),
        _item("b-only", "b", "2026-09-01T00:00:00Z", 2),
        _item("a-mid", "a", "2026-09-01T00:00:00Z", 3),
    ]
    assert sorted(find_duplicates(items)) == ["a-late", "a-mid"]


def test_dedupe_stops_at_quota_and_resumes_next_run(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "d.db"))
    monkeypatch.setattr("better_yt_playlist.dedupe.PROJECTS", ["default"])
    monkeypatch.setattr("better_yt_playlist.dedupe.DAILY_QUOTA", 1 + 2 * 50)  # list + 2 deletes
    conn = db.connect()
    fake = PlaylistFake(
        [_item("keep", "v", "2026-08-29T00:00:00Z", 0)]
        + [_item(f"dup{i}", "v", f"2026-09-0{i + 1}T00:00:00Z", i + 1) for i in range(3)]
    )
    monkeypatch.setattr("better_yt_playlist.dedupe.get_client", lambda project="default": fake)

    first = dedupe(PL, conn=conn)
    assert first == {"found": 3, "deleted": 2, "remaining": 1}

    conn.execute("DELETE FROM quota_log")  # next PT day
    second = dedupe(PL, conn=conn)
    assert second == {"found": 1, "deleted": 1, "remaining": 0}
    assert [it["id"] for it in fake.items] == ["keep"]
