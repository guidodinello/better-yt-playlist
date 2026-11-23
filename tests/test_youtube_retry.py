"""Reads ride out a network blip; writes are never retried.

Uses googleapiclient's real ``HttpRequest`` so the test checks the library's
retry loop, not a fake of it. Only the transport below it is stubbed.
"""

from __future__ import annotations

import json

import httplib2
import pytest
from googleapiclient.http import HttpRequest

from better_yt_playlist import db, youtube
from better_yt_playlist.db import Quota


class _FlakyHttp:
    """Times out on connect ``failures`` times, then answers 200."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def request(self, *_args, **_kwargs) -> tuple[httplib2.Response, bytes]:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("timed out")
        return httplib2.Response({"status": "200"}), b'{"items": []}'


def _request(http: _FlakyHttp) -> HttpRequest:
    req = HttpRequest(http, lambda _resp, content: json.loads(content), "https://example.test/x")
    req._sleep = lambda _s: None  # no real backoff in tests
    return req


@pytest.fixture
def quota(tmp_path, monkeypatch) -> Quota:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "r.db"))
    return Quota(db.connect())


def test_read_retries_after_a_connect_timeout(quota: Quota) -> None:
    http = _FlakyHttp(failures=2)
    response = youtube._execute(
        _request(http), quota, 1, "playlistItems.list", retries=youtube.READ_RETRIES
    )
    assert response == {"items": []}
    assert http.calls == 3
    assert quota.used_today() == 1


def test_write_is_not_retried(quota: Quota) -> None:
    http = _FlakyHttp(failures=1)
    with pytest.raises(TimeoutError):
        youtube._execute(_request(http), quota, 50, "playlistItems.insert")
    assert http.calls == 1
    assert quota.used_today() == 0
