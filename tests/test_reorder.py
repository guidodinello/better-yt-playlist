"""The reorder planner: correctness and the N - LIS move-count bound."""

from __future__ import annotations

import random

import pytest

from better_yt_playlist.reorder import OrderMismatch, compute_moves, lis_indices


def apply_moves(start: list[str], moves) -> list[str]:
    """Replay moves with real "remove then insert at index" semantics."""
    cur = list(start)
    for m in moves:
        cur.remove(m.playlist_item_id)
        cur.insert(m.position, m.playlist_item_id)
    return cur


def lis_len(actual: list[str], target: list[str]) -> int:
    rank = {pid: i for i, pid in enumerate(target)}
    return len(lis_indices([rank[p] for p in actual]))


@pytest.mark.parametrize("n", [1, 2, 5, 12, 40, 100])
def test_converges_and_respects_bound(n: int) -> None:
    rng = random.Random(n)
    target = [f"i{k}" for k in range(n)]
    for _ in range(30):
        actual = target[:]
        rng.shuffle(actual)
        moves = compute_moves(actual, target)
        assert apply_moves(actual, moves) == target
        assert len(moves) <= n - lis_len(actual, target)


def test_already_sorted_needs_no_moves() -> None:
    target = [f"i{k}" for k in range(10)]
    assert compute_moves(target[:], target) == []


def test_single_tail_item_is_one_move() -> None:
    # [B,C,A] -> [A,B,C]: LIS is [B,C], so exactly one move.
    moves = compute_moves(["B", "C", "A"], ["A", "B", "C"])
    assert len(moves) == 1
    assert apply_moves(["B", "C", "A"], moves) == ["A", "B", "C"]


def test_resume_from_partial_progress() -> None:
    rng = random.Random(0)
    target = [f"i{k}" for k in range(25)]
    actual = target[:]
    rng.shuffle(actual)

    # Apply only the first few moves, then re-plan from that state (simulating
    # a run that stopped on a quota budget, followed by a fresh invocation).
    first = compute_moves(actual, target)[:3]
    midway = apply_moves(actual, first)
    rest = compute_moves(midway, target)
    assert apply_moves(midway, rest) == target


def test_mismatched_sets_raise() -> None:
    with pytest.raises(OrderMismatch):
        compute_moves(["A", "B"], ["A", "B", "C"])
    with pytest.raises(OrderMismatch):
        compute_moves(["A", "X"], ["A", "B"])


def test_duplicate_video_distinct_item_ids_are_independent() -> None:
    # Same video twice => two playlist_item_ids; planner treats them as distinct.
    actual = ["pi3", "pi1", "pi2"]
    target = ["pi1", "pi2", "pi3"]
    moves = compute_moves(actual, target)
    assert apply_moves(actual, moves) == target
