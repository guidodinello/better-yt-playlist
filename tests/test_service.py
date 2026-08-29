"""run_reorder: budget gating, convergence, and recovery from failing moves."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from googleapiclient.errors import HttpError

from better_yt_playlist import db
from better_yt_playlist.service import run_reorder

Json = dict[str, Any]


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


class ReorderFake:
    """In-memory playlist whose ``update`` applies real "remove then insert" shifts.

    Any id in ``poison`` raises ``HttpError(400)`` instead of moving, every time.
    """

    def __init__(self, order: list[str], poison: set[str] | None = None) -> None:
        self.order = list(order)
        self.poison = poison or set()
        self.update_calls = 0

    def playlistItems(self) -> ReorderFake:  # noqa: N802
        return self

    def list(self, **_kw: object) -> _Req:  # noqa: A003
        items = [
            {
                "id": pid,
                "snippet": {"position": i, "resourceId": {"videoId": f"v{pid}"}},
                "contentDetails": {"videoId": f"v{pid}"},
            }
            for i, pid in enumerate(self.order)
        ]
        return _Req({"items": items})

    def list_next(self, *_a: object) -> None:
        return None

    def update(self, *, part: str, body: Json) -> _Req | _Raiser:  # noqa: A003
        self.update_calls += 1
        pid = str(body["id"])
        if pid in self.poison:
            return _Raiser(HttpError(_Resp(400), b"bad"))  # type: ignore[arg-type]
        pos = int(body["snippet"]["position"])
        self.order.remove(pid)
        self.order.insert(pos, pid)
        return _Req({})


def _seed_target(conn: sqlite3.Connection, target: list[str]) -> None:
    conn.execute("DELETE FROM target_order")
    conn.execute("DELETE FROM playlist_items")
    conn.executemany(
        "INSERT INTO target_order (playlist_id, rank, playlist_item_id) VALUES ('PL', ?, ?)",
        list(enumerate(target)),
    )
    # one synced row per id so _single_playlist_id / permutation checks pass
    conn.executemany(
        "INSERT INTO playlist_items "
        "(playlist_item_id, playlist_id, video_id, position, synced_at) "
        "VALUES (?, 'PL', ?, 0, 'now')",
        [(pid, f"v{pid}") for pid in target],
    )
    conn.commit()


def test_reorder_converges(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "r.db"))
    conn = db.connect()
    target = ["a", "b", "c", "d", "e"]
    _seed_target(conn, target)
    fake = ReorderFake(["c", "e", "a", "d", "b"])
    run_reorder(budget=9500, conn=db.connect(), client=fake)
    assert fake.order == target


def test_reorder_stops_cleanly_on_failed_move(tmp_path: Any, monkeypatch: Any, caplog: Any) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "r.db"))
    conn = db.connect()
    target = ["a", "b", "c", "d", "e"]
    _seed_target(conn, target)
    # `d` cannot be moved. The run must stop at that move, not keep applying
    # later positions against a live list that has diverged from the plan.
    live = ["c", "e", "a", "d", "b"]
    fake = ReorderFake(live, poison={"d"})

    from better_yt_playlist.reorder import compute_moves

    plan = compute_moves(live, target)
    idx_d = next(i for i, m in enumerate(plan) if m.playlist_item_id == "d")

    with caplog.at_level(logging.WARNING, logger="byp"):
        run_reorder(budget=9500, conn=db.connect(), client=fake)

    assert any("move failed for d" in r.message for r in caplog.records)
    # exactly the moves before d's were attempted (its move counts as attempt N+1)
    assert fake.update_calls == idx_d + 1
    # and the prefix that did apply is a correct partial application of the plan
    expected = list(live)
    for m in plan[:idx_d]:
        expected.remove(m.playlist_item_id)
        expected.insert(m.position, m.playlist_item_id)
    assert fake.order == expected


def test_reorder_resumes_after_poison_item_removed_from_target(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "r.db"))
    conn = db.connect()
    _seed_target(conn, ["a", "b", "c", "d", "e"])
    run_reorder(
        budget=9500,
        conn=db.connect(),
        client=ReorderFake(["c", "e", "a", "d", "b"], poison={"d"}),
    )

    # User re-runs `order-from-query` with the unmovable item filtered out,
    # which just rewrites target_order; the rest then converges around it.
    _seed_target(db.connect(), ["a", "b", "c", "e"])
    fake = ReorderFake(["c", "e", "a", "b"])
    run_reorder(budget=9500, conn=db.connect(), client=fake)
    assert fake.order == ["a", "b", "c", "e"]


def test_reorder_budget_gate_avoids_listing(tmp_path: Any, monkeypatch: Any) -> None:
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
    assert fake.update_calls == 0  # bailed before touching the API
