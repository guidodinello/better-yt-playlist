"""Facts about a mirrored playlist: size, channels, durations, age, popularity.

Read-only over the local DB (no quota). Counts only live entries (not removed).
When ``watch_history`` has rows (``byp import-history``), per-channel lines
also say how many of the channel's videos were already watched (opened).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import connect

TOP = 10
# (label, upper bound in seconds, exclusive); the last bucket has no bound.
DURATION_BUCKETS = (("<=1m", 61), ("1-10m", 600), ("10-30m", 1800), ("30-60m", 3600), ("1h+", None))

_LIVE = "playlist_id = :pl AND removed_at IS NULL"


def _hours(seconds: float | None) -> float:
    return round((seconds or 0) / 3600, 1)


def playlist_stats(conn: sqlite3.Connection, playlist_id: str) -> dict[str, Any]:
    """Every metric as plain data, so the CLI and tests share one source."""
    p = {"pl": playlist_id}

    def rows(sql: str) -> list[sqlite3.Row]:
        return conn.execute(sql, p).fetchall()

    def one(sql: str) -> sqlite3.Row:
        return conn.execute(sql, p).fetchone()

    has_history = conn.execute("SELECT EXISTS (SELECT 1 FROM watch_history)").fetchone()[0]
    watched = "video_id IN (SELECT video_id FROM watch_history)"

    total = one(
        f"SELECT count(*) n, sum(duration_s) secs, count(DISTINCT channel_id) channels, "
        f"sum({watched}) watched, sum(CASE WHEN {watched} THEN duration_s END) watched_secs "
        f"FROM playlist_items WHERE {_LIVE}"
    )
    n = total["n"]
    durations = [
        r[0]
        for r in rows(
            f"SELECT duration_s FROM playlist_items WHERE {_LIVE} AND duration_s IS NOT NULL "
            "ORDER BY duration_s"
        )
    ]
    buckets = dict.fromkeys((label for label, _ in DURATION_BUCKETS), 0)
    for d in durations:
        for label, bound in DURATION_BUCKETS:
            if bound is None or d < bound:
                buckets[label] += 1
                break

    channel_sql = (
        f"SELECT channel_title, count(*) n, sum(duration_s) secs, sum({watched}) watched "
        f"FROM playlist_items WHERE {_LIVE} AND channel_id IS NOT NULL GROUP BY channel_id "
    )
    return {
        "videos": n,
        "hours": _hours(total["secs"]),
        "days": round((total["secs"] or 0) / 86400, 1),
        "avg_min": round((total["secs"] or 0) / n / 60, 1) if n else 0,
        "median_min": round(durations[len(durations) // 2] / 60, 1) if durations else 0,
        "channels": total["channels"],
        "one_video_channels": one(
            f"SELECT count(*) FROM (SELECT 1 FROM playlist_items WHERE {_LIVE} "
            "AND channel_id IS NOT NULL GROUP BY channel_id HAVING count(*) = 1)"
        )[0],
        "has_history": bool(has_history),
        "watched": total["watched"] or 0,
        "watched_hours": _hours(total["watched_secs"]),
        "buckets": buckets,
        "top_by_videos": [dict(r) for r in rows(channel_sql + f"ORDER BY n DESC LIMIT {TOP}")],
        "top_by_hours": [dict(r) for r in rows(channel_sql + f"ORDER BY secs DESC LIMIT {TOP}")],
        "longest": [
            dict(r)
            for r in rows(
                f"SELECT title, channel_title, duration_s FROM playlist_items WHERE {_LIVE} "
                "AND duration_s IS NOT NULL ORDER BY duration_s DESC LIMIT 5"
            )
        ],
        "by_year": [
            tuple(r)
            for r in rows(
                f"SELECT substr(published_at, 1, 4) y, count(*) FROM playlist_items "
                f"WHERE {_LIVE} AND published_at IS NOT NULL GROUP BY y ORDER BY y"
            )
        ],
        "oldest": [
            dict(r)
            for r in rows(
                f"SELECT published_at, title, channel_title FROM playlist_items WHERE {_LIVE} "
                "AND published_at IS NOT NULL ORDER BY published_at LIMIT 3"
            )
        ],
        "most_viewed": [
            dict(r)
            for r in rows(
                f"SELECT view_count, title, channel_title FROM playlist_items WHERE {_LIVE} "
                "AND view_count IS NOT NULL ORDER BY view_count DESC LIMIT 5"
            )
        ],
        "least_viewed": [
            dict(r)
            for r in rows(
                f"SELECT view_count, title, channel_title FROM playlist_items WHERE {_LIVE} "
                "AND view_count IS NOT NULL ORDER BY view_count LIMIT 3"
            )
        ],
        "added_by_month": [
            tuple(r)
            for r in rows(
                f"SELECT substr(added_at, 1, 7) m, count(*) FROM playlist_items "
                f"WHERE {_LIVE} AND added_at IS NOT NULL GROUP BY m ORDER BY m"
            )
        ],
    }


def _channel_line(c: dict[str, Any], has_history: bool) -> str:
    line = f"  {c['channel_title']}: {c['n']} videos, {_hours(c['secs'])} h"
    if has_history:
        line += f", {c['watched']} watched ({round(100 * c['watched'] / c['n'])}%)"
    return line


def render(s: dict[str, Any]) -> str:
    """The stats as plain text for the terminal."""
    if not s["videos"]:
        return "no live videos in that playlist (has it been synced?)"
    h = s["has_history"]
    out = [
        f"{s['videos']} videos, {s['hours']} h ({s['days']} days nonstop)",
        f"average {s['avg_min']} min, median {s['median_min']} min",
        f"{s['channels']} channels, {s['one_video_channels']} with a single video",
    ]
    if h:
        out.append(f"already watched (opened): {s['watched']} videos, {s['watched_hours']} h")
    out.append("durations: " + ", ".join(f"{k} {v}" for k, v in s["buckets"].items()))
    out.append("\ntop channels by videos:")
    out += [_channel_line(c, h) for c in s["top_by_videos"]]
    out.append("\ntop channels by hours:")
    out += [_channel_line(c, h) for c in s["top_by_hours"]]
    out.append("\nlongest:")
    out += [
        f"  {_hours(v['duration_s'])} h  {v['title']} ({v['channel_title']})" for v in s["longest"]
    ]
    out.append("\nby year published: " + ", ".join(f"{y} {n}" for y, n in s["by_year"]))
    out.append("oldest:")
    out += [f"  {v['published_at'][:10]}  {v['title']} ({v['channel_title']})" for v in s["oldest"]]
    out.append("\nmost viewed:")
    out += [f"  {v['view_count']:,}  {v['title']} ({v['channel_title']})" for v in s["most_viewed"]]
    out.append("least viewed:")
    out += [
        f"  {v['view_count']:,}  {v['title']} ({v['channel_title']})" for v in s["least_viewed"]
    ]
    out.append("\nadded by month: " + ", ".join(f"{m} {n}" for m, n in s["added_by_month"]))
    return "\n".join(out)


def stats(playlist_id: str, conn: sqlite3.Connection | None = None) -> str:
    return render(playlist_stats(conn or connect(), playlist_id))
