# pyright: basic
"""Thin wrappers over the YouTube Data API v3 calls this tool makes.

Every call that costs quota routes through a :class:`~better_yt_playlist.db.Quota`
so the running total is always persisted.

Quota costs (verified against Google's quota calculator):
    playlistItems.list    1
    videos.list           1
    playlistItems.update  50
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

from googleapiclient.errors import HttpError

from .db import Quota


class QuotaExceeded(RuntimeError):
    """Raised when the API reports the daily quota is exhausted."""


class ApiError(RuntimeError):
    """Any other non-success response from the API (4xx/5xx that isn't quota).

    Wraps the underlying ``HttpError`` so callers need not import googleapiclient.
    """


def is_quota_exceeded(exc: HttpError) -> bool:
    return exc.resp.status == 403 and b"quotaExceeded" in (exc.content or b"")


_DURATION_RE = re.compile(
    r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
)


def parse_duration(iso: str | None) -> int | None:
    """Convert an ISO-8601 duration (``PT4M13S``) to whole seconds."""
    if not iso:
        return None
    m = _DURATION_RE.fullmatch(iso)
    if not m or iso == "P":
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def parse_playlist_id(value: str) -> str:
    """Accept a bare playlist id or any YouTube URL carrying ``list=``."""
    if "list=" in value:
        params = parse_qs(urlparse(value).query)
        if "list" in params:
            return params["list"][0]
    return value.strip()


def iter_playlist_items(client: Any, playlist_id: str, quota: Quota) -> Iterator[dict[str, Any]]:
    """Yield every ``playlistItem`` resource, following pagination."""
    request = client.playlistItems().list(
        part="snippet,contentDetails",
        playlistId=playlist_id,
        maxResults=50,
    )
    while request is not None:
        response = _execute(request, quota, 1, "playlistItems.list")
        yield from response.get("items", [])
        request = client.playlistItems().list_next(request, response)


def fetch_live_order(client: Any, playlist_id: str, quota: Quota) -> list[tuple[str, str]]:
    """Return ``(playlist_item_id, video_id)`` pairs in current playlist order.

    The API yields items in order already; we still sort by ``snippet.position``
    so the result does not depend on that undocumented guarantee.
    """
    items = sorted(
        iter_playlist_items(client, playlist_id, quota),
        key=lambda it: it.get("snippet", {}).get("position", 0),
    )
    return [(it["id"], _pi_video_id(it)) for it in items]


def _pi_video_id(item: dict[str, Any]) -> str:
    details = item.get("contentDetails", {})
    snippet = item.get("snippet", {})
    return details.get("videoId") or snippet.get("resourceId", {}).get("videoId", "")


def fetch_videos(client: Any, video_ids: list[str], quota: Quota) -> dict[str, dict[str, Any]]:
    """Fetch video resources in batches of 50, keyed by video id.

    Videos that no longer exist (deleted / private) are simply absent from
    the result — callers must treat this as a left join.
    """
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(video_ids), 50):
        chunk = video_ids[start : start + 50]
        request = client.videos().list(
            part="contentDetails,snippet,statistics",
            id=",".join(chunk),
            maxResults=50,
        )
        response = _execute(request, quota, 1, "videos.list")
        for item in response.get("items", []):
            out[item["id"]] = item
    return out


def move_item(
    client: Any,
    quota: Quota,
    *,
    playlist_item_id: str,
    playlist_id: str,
    video_id: str,
    position: int,
) -> None:
    """Move an existing playlist item to ``position`` (0-based).

    ``playlistItems.update`` replaces the resource, so the body must carry
    every field we want to keep, not just ``position``.
    """
    body = {
        "id": playlist_item_id,
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
            "position": position,
        },
    }
    request = client.playlistItems().update(part="snippet", body=body)
    _execute(request, quota, 50, "playlistItems.update")


def _execute(request: Any, quota: Quota, units: int, method: str) -> dict[str, Any]:
    try:
        response = request.execute()
    except HttpError as exc:
        if is_quota_exceeded(exc):
            raise QuotaExceeded(method) from exc
        raise ApiError(f"{method}: {exc}") from exc
    quota.charge(units, method)
    return response
