"""Compute and apply the minimum set of moves to reach a target order.

A ``playlistItems.update`` with a ``position`` is a *move* on a live list:
every item between the source and destination shifts. So we never store a
precomputed move list — it would be invalidated by its own first call.
Instead the durable artifact is the target order (the ``target_order``
table), and each run recomputes what to move from freshly fetched remote
state.

Each move costs 50 quota units, so the count matters directly. The minimum
number of "move to position" operations to turn one permutation into another
is ``N - L`` where ``L`` is the length of the longest subsequence already in
the correct relative order. We keep one such subsequence fixed and move only
the complement.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass


class OrderMismatch(RuntimeError):
    """The live playlist and the stored target order are not the same set."""


def lis_indices(seq: Sequence[int]) -> list[int]:
    """Indices of one longest strictly-increasing subsequence of ``seq``."""
    tails: list[int] = []
    tails_idx: list[int] = []
    prev = [-1] * len(seq)
    for i, value in enumerate(seq):
        pos = bisect.bisect_left(tails, value)
        if pos == len(tails):
            tails.append(value)
            tails_idx.append(i)
        else:
            tails[pos] = value
            tails_idx[pos] = i
        prev[i] = tails_idx[pos - 1] if pos > 0 else -1

    result: list[int] = []
    k = tails_idx[-1] if tails_idx else -1
    while k != -1:
        result.append(k)
        k = prev[k]
    result.reverse()
    return result


@dataclass(frozen=True, slots=True)
class Move:
    playlist_item_id: str
    position: int  # 0-based index the item should have after the move


def compute_moves(actual: Sequence[str], target: Sequence[str]) -> list[Move]:
    """Moves that transform ``actual`` into ``target``.

    Both arguments are sequences of ``playlist_item_id``. They must describe
    the same multiset of ids; otherwise :class:`OrderMismatch` is raised and
    the caller should re-sync and rebuild the target order.

    The returned moves are ordered and assume each is applied before the
    next (i.e. positions account for the shifts caused by earlier moves).
    Guarantees ``len(result) <= len(target) - len(lis)``.
    """
    if len(actual) != len(target) or set(actual) != set(target):
        raise OrderMismatch(
            f"live playlist has {len(actual)} items, target order has "
            f"{len(target)}; sets differ. Re-run `sync` then `order-from-query`."
        )

    rank = {pid: i for i, pid in enumerate(target)}
    ranks_in_actual = [rank[pid] for pid in actual]
    keep = {actual[i] for i in lis_indices(ranks_in_actual)}

    current = list(actual)
    placed = set(keep)
    moves: list[Move] = []

    for pid in sorted((p for p in target if p not in keep), key=lambda p: rank[p]):
        r = rank[pid]
        preds = [p for p in placed if rank[p] < r]
        if preds:
            pred = max(preds, key=lambda p: rank[p])
            insert_at = current.index(pred) + 1
        else:
            insert_at = 0

        cur = current.index(pid)
        current.pop(cur)
        if cur < insert_at:
            insert_at -= 1

        if cur != insert_at:
            current.insert(insert_at, pid)
            moves.append(Move(pid, insert_at))
        else:
            current.insert(insert_at, pid)
        placed.add(pid)

    return moves
