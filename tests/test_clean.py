"""clean: duplicate copies and deleted/private videos are removed from the playlist."""

from __future__ import annotations

from typing import Any

from better_yt_playlist import db
from better_yt_playlist.clean import clean, find_duplicates, find_unavailable

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


class _Videos:
    """``videos().list`` that only returns ids in ``available``, like the real API."""

    def __init__(self, available: set[str] | None) -> None:
        self.available = available

    def list(self, *, id: str, **_: Any) -> _Req:  # noqa: A002, A003
        ids = id.split(",")
        found = ids if self.available is None else [v for v in ids if v in self.available]
        return _Req({"items": [{"id": v} for v in found]})


class PlaylistFake:
    """Fake client serving a live playlist that ``delete`` actually shrinks.

    ``available`` limits which videos ``videos().list`` returns; ``None`` means all.
    """

    def __init__(self, items: list[Json], available: set[str] | None = None) -> None:
        self.items = items
        self.deleted: list[str] = []
        self._videos = _Videos(available)

    def videos(self) -> _Videos:
        return self._videos

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


def test_find_unavailable_returns_every_copy_of_a_missing_video() -> None:
    items = [
        _item("ok", "live", "2026-08-29T00:00:00Z", 0),
        _item("gone-1", "private", "2026-08-29T00:00:00Z", 1),
        _item("gone-2", "private", "2026-09-01T00:00:00Z", 2),
    ]
    assert find_unavailable(items, available={"live"}) == ["gone-1", "gone-2"]


def test_clean_removes_unavailable_and_duplicates_once_each(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "d.db"))
    monkeypatch.setattr("better_yt_playlist.clean.PROJECTS", ["default"])
    conn = db.connect()
    fake = PlaylistFake(
        [
            _item("keep", "live", "2026-08-29T00:00:00Z", 0),
            _item("live-dup", "live", "2026-09-01T00:00:00Z", 1),
            _item("gone", "private", "2026-08-29T00:00:00Z", 2),
            _item("gone-dup", "private", "2026-09-01T00:00:00Z", 3),
        ],
        available={"live"},
    )
    monkeypatch.setattr("better_yt_playlist.clean.get_client", lambda project="default": fake)

    result = clean(PL, conn=conn)
    assert result == {"found": 3, "deleted": 3, "remaining": 0}
    assert sorted(fake.deleted) == ["gone", "gone-dup", "live-dup"]  # no double delete
    assert [it["id"] for it in fake.items] == ["keep"]


def test_clean_stops_at_quota_and_resumes_next_run(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "d.db"))
    monkeypatch.setattr("better_yt_playlist.clean.PROJECTS", ["default"])
    # listing + video lookup + 2 deletes
    monkeypatch.setattr("better_yt_playlist.clean.DAILY_QUOTA", 1 + 1 + 2 * 50)
    conn = db.connect()
    fake = PlaylistFake(
        [_item("keep", "v", "2026-08-29T00:00:00Z", 0)]
        + [_item(f"dup{i}", "v", f"2026-09-0{i + 1}T00:00:00Z", i + 1) for i in range(3)]
    )
    monkeypatch.setattr("better_yt_playlist.clean.get_client", lambda project="default": fake)

    first = clean(PL, conn=conn)
    assert first == {"found": 3, "deleted": 2, "remaining": 1}

    conn.execute("DELETE FROM quota_log")  # next PT day
    second = clean(PL, conn=conn)
    assert second == {"found": 1, "deleted": 1, "remaining": 0}
    assert [it["id"] for it in fake.items] == ["keep"]
