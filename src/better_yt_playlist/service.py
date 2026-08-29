"""Orchestration tying the API, the database, and the reorder planner together.

These functions own the quota-budget bookkeeping and the console output; the
planning maths lives in :mod:`better_yt_playlist.reorder`.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from googleapiclient.errors import HttpError

from .db import DAILY_QUOTA, Quota, connect
from .reorder import Move, compute_moves
from .youtube import QuotaExceeded, fetch_live_order, move_item

logger = logging.getLogger("byp")

MOVE_COST = 50
MOVES_PER_DAY = DAILY_QUOTA // MOVE_COST


def _client() -> Any:
    from .auth import get_client

    return get_client()


def _single_playlist_id(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT DISTINCT playlist_id FROM playlist_items WHERE removed_at IS NULL"
    ).fetchall()
    if not rows:
        raise SystemExit("no synced playlist found — run `byp sync <playlist>` first")
    if len(rows) > 1:
        raise SystemExit(
            "database holds more than one playlist; this tool assumes one. "
            f"Found: {', '.join(r[0] for r in rows)}"
        )
    return rows[0][0]


def save_target_order(item_ids: list[str], conn: sqlite3.Connection | None = None) -> None:
    """Persist a desired order, after checking it is a permutation of the live set."""
    conn = conn or connect()
    playlist_id = _single_playlist_id(conn)
    live = {
        r[0]
        for r in conn.execute(
            "SELECT playlist_item_id FROM playlist_items "
            "WHERE playlist_id = ? AND removed_at IS NULL",
            (playlist_id,),
        )
    }
    given = set(item_ids)
    if len(item_ids) != len(given):
        raise SystemExit("target order contains duplicate ids")
    if given != live:
        raise SystemExit(
            "target order is not a permutation of the synced playlist "
            f"({len(live - given)} missing, {len(given - live)} unknown). Make sure "
            "the query selects every non-removed row exactly once."
        )

    with conn:
        conn.execute("DELETE FROM target_order WHERE playlist_id = ?", (playlist_id,))
        conn.executemany(
            "INSERT INTO target_order (playlist_id, rank, playlist_item_id) VALUES (?, ?, ?)",
            [(playlist_id, rank, pid) for rank, pid in enumerate(item_ids)],
        )
    logger.info(f"saved target order for {len(item_ids)} items")


def _stored_target_order(conn: sqlite3.Connection, playlist_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT playlist_item_id FROM target_order WHERE playlist_id = ? ORDER BY rank",
        (playlist_id,),
    ).fetchall()
    if not rows:
        raise SystemExit('no target order stored — run `byp order-from-query "..."`')
    return [r[0] for r in rows]


def _plan(
    conn: sqlite3.Connection, client: Any, quota: Quota
) -> tuple[str, list[tuple[str, str]], list[Move]]:
    """Fetch live order once, compute the outstanding moves against the target."""
    playlist_id = _single_playlist_id(conn)
    live_pairs = fetch_live_order(client, playlist_id, quota)
    live_ids = [pid for pid, _ in live_pairs]
    target = _stored_target_order(conn, playlist_id)
    moves = compute_moves(live_ids, target)
    return playlist_id, live_pairs, moves


def reorder_status(conn: sqlite3.Connection | None = None, client: Any | None = None) -> None:
    conn = conn or connect()
    client = client or _client()
    quota = Quota(conn)
    _, live_pairs, moves = _plan(conn, client, quota)

    used = quota.used_today()
    logger.info(f"playlist size:       {len(live_pairs)}")
    logger.info(f"moves remaining:     {len(moves)}  (~{len(moves) * MOVE_COST} quota units)")
    logger.info(f"quota used today:    {used} / {DAILY_QUOTA}  (resets midnight PT)")
    if moves:
        days = -(-len(moves) // MOVES_PER_DAY)
        logger.info(f"est. days to finish: {days}  (max {MOVES_PER_DAY} moves/day)")
    else:
        logger.info("playlist already matches the target order.")


def run_reorder(
    budget: int,
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
    client: Any | None = None,
) -> None:
    conn = conn or connect()
    quota = Quota(conn)
    cap = min(budget, DAILY_QUOTA)

    # Check the budget before spending ~N/50 units listing the playlist, so a
    # re-run after the day's quota is gone costs nothing.
    if not dry_run and quota.used_today() + MOVE_COST > cap:
        logger.info(
            f"quota budget reached for today ({quota.used_today()} used, cap {cap}). "
            "Try again after midnight PT."
        )
        return

    client = client or _client()
    playlist_id, live_pairs, moves = _plan(conn, client, quota)
    video_of = dict(live_pairs)

    if not moves:
        logger.info("nothing to do — playlist already matches the target order.")
        return

    if dry_run:
        logger.info(f"{len(moves)} move(s) planned (dry run):")
        for m in moves:
            logger.info(f"  move {m.playlist_item_id} -> position {m.position}")
        return

    done = 0
    failed = 0
    try:
        for m in moves:
            if quota.used_today() + MOVE_COST > cap:
                logger.info(f"stopping: next move would exceed today's budget ({cap} units).")
                break
            try:
                move_item(
                    client,
                    quota,
                    playlist_item_id=m.playlist_item_id,
                    playlist_id=playlist_id,
                    video_id=video_of[m.playlist_item_id],
                    position=m.position,
                )
            except HttpError as exc:
                # A dead/private entry, or one changed underneath us: skip it
                # (50 units already spent) rather than aborting the whole run.
                failed += 1
                logger.warning("skipped %s: %s", m.playlist_item_id, exc)
                continue
            done += 1
            logger.info(f"  [{done}/{len(moves)}] moved {m.playlist_item_id} -> {m.position}")
    except QuotaExceeded:
        logger.info("YouTube reports the daily quota is exhausted — stopping cleanly.")

    left = len(moves) - done - failed
    logger.info(f"\napplied {done} move(s), ~{quota.session_units} units this run.")
    if failed:
        logger.info(f"{failed} move(s) failed and were skipped (see warnings above).")
    if left:
        logger.info(f"{left} move(s) remain — re-run `byp reorder` (tomorrow if quota is spent).")
    elif not failed:
        logger.info("target order reached. Run `byp sync` to refresh local positions.")
