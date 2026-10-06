"""lastfm: response parsing, error classification, retry/backoff and pacing."""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from better_yt_playlist.lastfm import (
    API_KEY_ENV,
    HttpResponse,
    LastfmAuthError,
    LastfmClient,
    LastfmError,
    LastfmRateLimited,
    RetryPolicy,
    parse_retry_after,
)


class ScriptedTransport:
    """Returns the scripted responses in order and records each requested URL."""

    def __init__(self, *responses: tuple[int, Any]) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str) -> HttpResponse:
        self.urls.append(url)
        status, body = self._responses.pop(0)
        return HttpResponse(status, json.dumps(body).encode(), {})


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def _client(transport: ScriptedTransport, time: FakeTime | None = None) -> LastfmClient:
    time = time or FakeTime()
    return LastfmClient(
        "k",
        transport=transport,
        policy=RetryPolicy(max_attempts=3, backoff_s=1.0, max_backoff_s=10.0),
        sleep=time.sleep,
        clock=time.clock,
        jitter=lambda: 0.5,
    )


def test_track_info_parses_canonical_names() -> None:
    transport = ScriptedTransport(
        (200, {"track": {"name": "Loser", "mbid": "", "url": "https://last.fm/t",
                         "artist": {"name": "Tame Impala", "mbid": "a1"}}}),
    )  # fmt: skip
    info = _client(transport).track_info("tame impala", "loser")
    assert info is not None
    assert (info.artist, info.track, info.mbid, info.url) == (
        "Tame Impala",
        "Loser",
        None,
        "https://last.fm/t",
    )
    assert "method=track.getInfo" in transport.urls[0]
    assert "autocorrect=1" in transport.urls[0]


@pytest.mark.parametrize("status", [200, 404])
def test_track_info_not_found_is_none(status: int) -> None:
    transport = ScriptedTransport((status, {"error": 6, "message": "Track not found"}))
    assert _client(transport).track_info("nobody", "nothing") is None


def test_similar_tracks_handles_single_object_and_string_match() -> None:
    one = {
        "name": "Feels Like We Only Go Backwards",
        "match": "0.87",
        "url": "u",
        "artist": {"name": "Tame Impala"},
    }
    transport = ScriptedTransport((200, {"similartracks": {"track": one}}))  # fmt: skip
    [t] = _client(transport).similar_tracks("Tame Impala", "Loser", limit=5)
    assert (t.artist, t.track, t.match) == ("Tame Impala", "Feels Like We Only Go Backwards", 0.87)


def test_search_track_reads_string_artist() -> None:
    transport = ScriptedTransport(
        (200, {"results": {"trackmatches": {"track": [{"name": "Fantasmas", "artist": "Miranda!",
                                                        "url": "u", "mbid": ""}]}}}),
    )  # fmt: skip
    [hit] = _client(transport).search_track("MIRANDA! Fantasmas")
    assert (hit.artist, hit.track) == ("Miranda!", "Fantasmas")


def test_rate_limit_retries_with_backoff_then_succeeds() -> None:
    time = FakeTime()
    transport = ScriptedTransport(
        (200, {"error": 29, "message": "Rate Limit Exceeded"}),
        (503, {"error": 16, "message": "temporarily unavailable"}),
        (200, {"similartracks": {"track": []}}),
    )
    assert _client(transport, time).similar_tracks("a", "b", limit=5) == []
    assert len(transport.urls) == 3
    # Exponential (1s, 2s) with the fixed jitter factor 0.9 + 0.2 * 0.5 = 1.0.
    backoffs = [s for s in time.sleeps if s >= 1.0]
    assert backoffs == [1.0, 2.0]


class FlakyNetwork(ScriptedTransport):
    """Raises a DNS-style URLError for the first ``failures`` calls."""

    def __init__(self, failures: int, *responses: tuple[int, Any]) -> None:
        super().__init__(*responses)
        self._failures = failures

    def __call__(self, url: str) -> HttpResponse:
        if self._failures:
            self._failures -= 1
            self.urls.append(url)
            raise urllib.error.URLError("[Errno -2] Name or service not known")
        return super().__call__(url)


def test_network_error_is_retried() -> None:
    transport = FlakyNetwork(2, (200, {"similartracks": {"track": []}}))
    assert _client(transport).similar_tracks("a", "b", limit=5) == []
    assert len(transport.urls) == 3


def test_persistent_network_error_raises_lastfm_error() -> None:
    with pytest.raises(LastfmError, match="network error"):
        _client(FlakyNetwork(3)).similar_tracks("a", "b", limit=5)


def test_rate_limit_exhausted_raises() -> None:
    transport = ScriptedTransport(*[(200, {"error": 29, "message": "slow down"})] * 3)
    with pytest.raises(LastfmRateLimited):
        _client(transport).similar_tracks("a", "b", limit=5)


@pytest.mark.parametrize("code", [10, 26])
def test_bad_key_raises_auth_error_without_retry(code: int) -> None:
    transport = ScriptedTransport((403, {"error": code, "message": "Invalid API key"}))
    with pytest.raises(LastfmAuthError):
        _client(transport).track_info("a", "b")
    assert len(transport.urls) == 1


def test_other_client_error_fails_without_retry() -> None:
    transport = ScriptedTransport((400, {"error": 13, "message": "Invalid method signature"}))
    with pytest.raises(LastfmError):
        _client(transport).track_info("a", "b")
    assert len(transport.urls) == 1


def test_calls_are_paced() -> None:
    time = FakeTime()
    transport = ScriptedTransport(*[(200, {"similartracks": {"track": []}})] * 3)
    client = _client(transport, time)
    for _ in range(3):
        client.similar_tracks("a", "b", limit=1)
    assert time.sleeps == [0.25, 0.25]


def test_from_env_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    with pytest.raises(LastfmAuthError, match=API_KEY_ENV):
        LastfmClient.from_env()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3", 3.0), ("0.5", 0.5), (None, None), ("abc", None), ("-1", None), ("inf", None),
     ("nan", None)],
)  # fmt: skip
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected
