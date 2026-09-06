"""import_remaining: permanently-failing videos are recorded and not retried."""

from __future__ import annotations

import sqlite3
from typing import Any

from googleapiclient.errors import HttpError

from better_yt_playlist import db
from better_yt_playlist.import_wl_to_yt import import_remaining

Json = dict[str, Any]

WL = "WL"
TARGET = "PLtarget"


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"


class _Req:
    def __init__(self, payload: Json) -> None:
        self.payload = payload

    def execute(self) -> Json:
        return self.payload


class _Raiser:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def execute(self) -> Json:
        raise self.exc


class InsertFake:
    """Fake client whose ``playlistItems().insert`` fails permanently for ``dead`` ids."""

    def __init__(self, dead: set[str] | None = None) -> None:
        self.dead = dead or set()
        self.insert_calls: list[str] = []

    def playlistItems(self) -> InsertFake:  # noqa: N802
        return self

    def insert(self, *, part: str, body: Json) -> _Req | _Raiser:  # noqa: A003
        vid = str(body["snippet"]["resourceId"]["videoId"])
        self.insert_calls.append(vid)
        if vid in self.dead:
            return _Raiser(HttpError(_Resp(404), b"video not found"))  # type: ignore[arg-type]
        return _Req({"id": f"{TARGET}-{vid}"})


def _seed_wl(conn: sqlite3.Connection, video_ids: list[str]) -> None:
    conn.executemany(
        "INSERT INTO playlist_items "
        "(playlist_item_id, playlist_id, video_id, position, synced_at) "
        "VALUES (?, ?, ?, ?, 'now')",
        [(f"{WL}-{vid}", WL, vid, i) for i, vid in enumerate(video_ids)],
    )
    conn.commit()


def test_permanently_failed_video_is_recorded_and_not_retried(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "wl.db"))
    conn = db.connect()
    _seed_wl(conn, ["good1", "dead1", "good2"])

    fake = InsertFake(dead={"dead1"})
    monkeypatch.setattr(
        "better_yt_playlist.import_wl_to_yt.get_client", lambda project="default": fake
    )

    result = import_remaining(TARGET, conn=conn)
    assert result["imported"] == 2
    assert result["skipped"] == 1
    assert fake.insert_calls == ["good1", "dead1", "good2"]

    failed = {
        r[0]
        for r in conn.execute(
            "SELECT video_id FROM import_failures WHERE target_playlist_id = ?", (TARGET,)
        ).fetchall()
    }
    assert failed == {"dead1"}

    # Re-run: the dead video must not be retried again.
    fake.insert_calls.clear()
    result2 = import_remaining(TARGET, conn=conn)
    assert result2["imported"] == 0
    assert fake.insert_calls == []
