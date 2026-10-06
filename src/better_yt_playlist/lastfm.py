# pyright: basic
"""Thin Last.fm API client: similar tracks, track lookup, track search.

Read-only methods that need only an API key (no user session). Responses are
parsed here, at the edge, into small frozen dataclasses so the rest of the
package never touches raw JSON.

Last.fm reports failures as ``{"error": <code>, "message": ...}`` — sometimes
with HTTP 200, sometimes with a 4xx — so both the status and the body are
classified. Codes (https://www.last.fm/api/errorcodes): an unknown track comes
back as HTTP 200 ``{"error": 6, "message": "Track not found"}`` (observed
live), so 6/7 mean *not found* here; 29 is the rate limit; 8/11/16 are
transient backend failures. The ToS gives no numeric rate limit, so calls are paced
conservatively and back off on 29.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Protocol, runtime_checkable

from . import __version__

API_URL = "https://ws.audioscrobbler.com/2.0/"
API_KEY_ENV = "BYP_LASTFM_API_KEY"
_USER_AGENT = f"better-yt-playlist/{__version__} (personal playlist tool)"
_TIMEOUT_S = 20.0

_NOT_FOUND_CODES = frozenset({6, 7})
_AUTH_CODES = frozenset({4, 10, 26})
_RATE_LIMIT_CODE = 29
_TRANSIENT_CODES = frozenset({8, 11, 16, _RATE_LIMIT_CODE})


class LastfmError(RuntimeError):
    """Any Last.fm failure the caller can't work around."""


class LastfmAuthError(LastfmError):
    """Missing, invalid or suspended API key."""


class LastfmRateLimited(LastfmError):
    """Still rate-limited (error 29) after every retry."""


@dataclass(frozen=True, slots=True)
class TrackInfo:
    artist: str
    track: str
    mbid: str | None
    url: str | None
    listeners: int | None = None


@dataclass(frozen=True, slots=True)
class SimilarTrack:
    artist: str
    track: str
    match: float
    mbid: str | None
    url: str | None


@runtime_checkable
class LastfmApi(Protocol):
    def track_info(self, artist: str, track: str) -> TrackInfo | None: ...
    def search_track(self, query: str, *, limit: int = 5) -> list[TrackInfo]: ...
    def similar_tracks(self, artist: str, track: str, *, limit: int) -> list[SimilarTrack]: ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


Transport = Callable[[str], HttpResponse]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    backoff_s: float = 1.0
    max_backoff_s: float = 30.0


def _urllib_transport(url: str) -> HttpResponse:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as resp:
            return HttpResponse(resp.status, resp.read(), dict(resp.headers))
    except urllib.error.HTTPError as exc:
        # Last.fm puts its JSON error body on 4xx/5xx responses too.
        return HttpResponse(exc.code, exc.read(), dict(exc.headers or {}))


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header; ``None`` if absent or unusable."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
        return None
    return seconds


class LastfmClient:
    """Satisfies :class:`LastfmApi` against the real web service."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport = _urllib_transport,
        policy: RetryPolicy | None = None,
        min_interval_s: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._policy = policy or RetryPolicy()
        self._min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._last_call: float | None = None

    @classmethod
    def from_env(cls) -> LastfmClient:
        key = os.environ.get(API_KEY_ENV)
        if not key:
            raise LastfmAuthError(
                f"no Last.fm API key — create one at https://www.last.fm/api/account/create "
                f"and export {API_KEY_ENV}"
            )
        return cls(key)

    def track_info(self, artist: str, track: str) -> TrackInfo | None:
        data = self._call("track.getInfo", artist=artist, track=track, autocorrect="1")
        if data is None or "track" not in data:
            return None
        t = data["track"]
        return TrackInfo(
            artist=_artist_name(t["artist"]),
            track=t["name"],
            mbid=t.get("mbid") or None,
            url=t.get("url") or None,
            listeners=_int_or_none(t.get("listeners")),
        )

    def search_track(self, query: str, *, limit: int = 5) -> list[TrackInfo]:
        data = self._call("track.search", track=query, limit=str(limit))
        if data is None:
            return []
        matches = data.get("results", {}).get("trackmatches", {}).get("track", [])
        return [
            TrackInfo(
                artist=_artist_name(m["artist"]),
                track=m["name"],
                mbid=m.get("mbid") or None,
                url=m.get("url") or None,
                listeners=_int_or_none(m.get("listeners")),
            )
            for m in _as_list(matches)
        ]

    def similar_tracks(self, artist: str, track: str, *, limit: int) -> list[SimilarTrack]:
        data = self._call(
            "track.getSimilar", artist=artist, track=track, autocorrect="1", limit=str(limit)
        )
        if data is None:
            return []
        items = data.get("similartracks", {}).get("track", [])
        return [
            SimilarTrack(
                artist=_artist_name(t["artist"]),
                track=t["name"],
                match=float(t["match"]),
                mbid=t.get("mbid") or None,
                url=t.get("url") or None,
            )
            for t in _as_list(items)
        ]

    def _pace(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            wait = self._last_call + self._min_interval_s - now
            if wait > 0:
                self._sleep(wait)
                now += wait
        self._last_call = now

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        base = min(self._policy.backoff_s * 2 ** (attempt - 1), self._policy.max_backoff_s)
        if retry_after is not None:
            return min(retry_after, self._policy.max_backoff_s)
        return base * (0.9 + 0.2 * self._jitter())

    def _call(self, method: str, **params: str) -> dict[str, Any] | None:
        """One API call with retries. ``None`` means Last.fm doesn't know the item."""
        query = urllib.parse.urlencode(
            {"method": method, "api_key": self._api_key, "format": "json", **params}
        )
        url = f"{API_URL}?{query}"
        code: int | None = None
        failure = "no attempt made"
        for attempt in range(1, self._policy.max_attempts + 1):
            self._pace()
            try:
                resp = self._transport(url)
            except OSError as exc:  # DNS failure, reset, timeout (URLError is an OSError)
                failure = f"network error: {exc}"
                if attempt == self._policy.max_attempts:
                    break
                self._sleep(self._backoff(attempt, None))
                continue
            data = _decode(resp.body)
            code = data.get("error") if isinstance(data, dict) else None
            if code is None and resp.status == HTTPStatus.OK and isinstance(data, dict):
                return data
            if code in _NOT_FOUND_CODES:
                return None
            if code in _AUTH_CODES:
                raise LastfmAuthError(f"Last.fm rejected the API key: {data.get('message')}")
            failure = f"HTTP {resp.status}, error {code}"
            retryable = code in _TRANSIENT_CODES or resp.status >= HTTPStatus.INTERNAL_SERVER_ERROR
            if not retryable or attempt == self._policy.max_attempts:
                break
            self._sleep(self._backoff(attempt, parse_retry_after(resp.headers.get("Retry-After"))))
        if code == _RATE_LIMIT_CODE:
            raise LastfmRateLimited(f"{method}: still rate-limited after retries")
        raise LastfmError(f"{method} failed ({failure})")


def _decode(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    """Last.fm's JSON collapses a one-element list into a bare object."""
    if isinstance(value, list):
        return value
    return [value] if value else []


def _artist_name(artist: Any) -> str:
    """``artist`` is an object in getInfo/getSimilar but a plain string in search."""
    return artist["name"] if isinstance(artist, dict) else str(artist)
