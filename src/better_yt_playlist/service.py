"""Orchestration tying the API, the database, and the reorder planner together.

These functions own the quota-budget bookkeeping and the console output; the
planning maths lives in :mod:`better_yt_playlist.reorder`.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from .db import DAILY_QUOTA, Quota, connect
from .reorder import Move, OrderMismatch, compute_moves
from .youtube import ApiError, QuotaExceeded, fetch_live_order, move_item

logger = logging.getLogger("byp")

MOVE_COST = 50
MOVES_PER_DAY = DAILY_QUOTA // MOVE_COST


def _client() -> Any:
    from .auth import get_client

    return get_client()


# The local Watch Later mirror is a separate data source (yt-dlp import), never a
# playlist the reorder tooling operates on, so it is excluded from the "single
# live playlist" check.
_MIRROR_PLAYLIST_IDS = {"WL"}


def _single_playlist_id(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT DISTINCT playlist_id FROM playlist_items "
        "WHERE removed_at IS NULL AND playlist_id NOT IN ({})".format(
            ", ".join("?" * len(_MIRROR_PLAYLIST_IDS))
        ),
        tuple(_MIRROR_PLAYLIST_IDS),
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
) -> tuple[str, dict[str, str], list[Move]]:
    """Fetch live order and compute the outstanding moves against the target."""
    playlist_id = _single_playlist_id(conn)
    live_pairs = fetch_live_order(client, playlist_id, quota)
    video_of = {pid: vid for pid, vid in live_pairs}
    live_ids = [pid for pid, _ in live_pairs]
    target = _stored_target_order(conn, playlist_id)
    try:
        moves = compute_moves(live_ids, target)
    except OrderMismatch as exc:
        raise SystemExit(str(exc)) from exc
    return playlist_id, video_of, moves


def reorder_status(conn: sqlite3.Connection | None = None, client: Any | None = None) -> None:
    conn = conn or connect()
    client = client or _client()
    quota = Quota(conn)
    _, video_of, moves = _plan(conn, client, quota)

    used = quota.used_today()
    logger.info(f"playlist size:       {len(video_of)}")
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

    if dry_run:
        _, _, moves = _plan(conn, client, quota)
        if not moves:
            logger.info("nothing to do — playlist already matches the target order.")
            return
        logger.info(f"{len(moves)} move(s) planned (dry run):")
        for m in moves:
            logger.info(f"  move {m.playlist_item_id} -> position {m.position}")
        return

    playlist_id, video_of, moves = _plan(conn, client, quota)
    if not moves:
        logger.info("nothing to do — playlist already matches the target order.")
        return

    # Every Move.position is computed assuming all earlier moves in the batch
    # landed (a move shifts the live list). So once a move fails, the rest of
    # the batch is invalid — stop rather than apply positions against a live
    # list that has diverged from the plan. The moves already applied are a
    # correct prefix; a later `byp reorder` re-plans from fresh state.
    applied = 0
    unmovable: str | None = None
    stopped: str | None = None

    try:
        for m in moves:
            if quota.used_today() + MOVE_COST > cap:
                stopped = "budget"
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
            except ApiError as exc:
                unmovable = m.playlist_item_id
                logger.warning("move failed for %s: %s", m.playlist_item_id, exc)
                break
            applied += 1
            logger.info(f"  [{applied}/{len(moves)}] moved {m.playlist_item_id} -> {m.position}")
    except QuotaExceeded:
        stopped = "quota"
        logger.info("YouTube reports the daily quota is exhausted — stopping cleanly.")

    logger.info(f"\napplied {applied} move(s), ~{quota.session_units} units this run.")
    if unmovable:
        logger.info(
            f"stopped: item {unmovable} could not be repositioned (likely a deleted or "
            "private video). Drop it from the order-from-query "
            f"(e.g. `... AND playlist_item_id != '{unmovable}'`), then re-run `byp reorder`."
        )
    elif stopped == "budget":
        logger.info(f"stopped at today's budget ({cap} units) — re-run `byp reorder` tomorrow.")
    elif stopped == "quota":
        logger.info("re-run `byp reorder` after the quota resets (midnight PT).")
    elif applied < len(moves):
        logger.info(f"{len(moves) - applied} move(s) remain — re-run `byp reorder`.")
    else:
        logger.info("target order reached. Run `byp sync` to refresh local positions.")
