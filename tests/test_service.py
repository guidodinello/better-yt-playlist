"""run_reorder: budget gating and resilience to a single failing move."""

from __future__ import annotations

import logging

from googleapiclient.errors import HttpError

from better_yt_playlist import db
from better_yt_playlist.reorder import compute_moves
from better_yt_playlist.service import run_reorder


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"


class ReorderFake:
    """In-memory playlist whose `update` applies real shift semantics.

    Any item id in ``poison`` raises HttpError(400) instead of moving.
    """

    def __init__(self, order: list[str], poison: set[str] | None = None) -> None:
        self.order = list(order)
        self.poison = poison or set()
        self.calls = 0

    def playlistItems(self):  # noqa: N802
        return self

    def list(self, **_kw):  # noqa: A003
        items = [
            {
                "id": pid,
                "snippet": {"position": i, "resourceId": {"videoId": f"v{pid}"}},
                "contentDetails": {"videoId": f"v{pid}"},
            }
            for i, pid in enumerate(self.order)
        ]
        return _Req({"items": items})

    def list_next(self, *_a):
        return None

    def update(self, *, part, body):  # noqa: A003
        self.calls += 1
        pid = body["id"]
        if pid in self.poison:
            return _Raiser(HttpError(_Resp(400), b"bad"))
        pos = body["snippet"]["position"]
        self.order.remove(pid)
        self.order.insert(pos, pid)
        return _Req({})


class _Req:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Raiser:
    def __init__(self, exc):
        self.exc = exc

    def execute(self):
        raise self.exc


def _seed_target(conn, target):
    conn.execute("DELETE FROM target_order")
    conn.executemany(
        "INSERT INTO target_order (playlist_id, rank, playlist_item_id) VALUES ('PL', ?, ?)",
        list(enumerate(target)),
    )
    # a synced row per id so _single_playlist_id works
    conn.executemany(
        "INSERT INTO playlist_items "
        "(playlist_item_id, playlist_id, video_id, position, synced_at) "
        "VALUES (?, 'PL', ?, 0, 'now')",
        [(pid, f"v{pid}") for pid in target],
    )
    conn.commit()


def test_reorder_converges(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "r.db"))
    conn = db.connect()
    target = ["a", "b", "c", "d", "e"]
    _seed_target(conn, target)
    fake = ReorderFake(["c", "e", "a", "d", "b"])
    run_reorder(budget=9500, conn=db.connect(), client=fake)
    assert fake.order == target


def test_reorder_skips_failing_move_and_continues(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "r.db"))
    conn = db.connect()
    target = ["a", "b", "c", "d", "e"]
    _seed_target(conn, target)
    fake = ReorderFake(["c", "e", "a", "d", "b"], poison={"c"})

    with caplog.at_level(logging.WARNING, logger="byp"):
        run_reorder(budget=9500, conn=db.connect(), client=fake)

    plan_len = len(compute_moves(["c", "e", "a", "d", "b"], target))
    assert any("skipped c" in r.message for r in caplog.records)
    # the run kept going after the failure: every planned move was attempted
    assert fake.calls == plan_len


def test_reorder_budget_gate_avoids_listing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "r.db"))
    conn = db.connect()
    _seed_target(conn, ["a", "b"])
    conn.execute(
        "INSERT INTO quota_log (ts, units, method) VALUES "
        "(strftime('%Y-%m-%dT%H:%M:%SZ','now'), 9999, 'x')"
    )
    conn.commit()

    fake = ReorderFake(["b", "a"])
    run_reorder(budget=9500, conn=db.connect(), client=fake)
    assert fake.calls == 0  # bailed before touching the API
