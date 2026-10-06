"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from better_yt_playlist import db


@pytest.fixture(autouse=True)
def _close_db_connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close every connection a test opens via ``db.connect``.

    The CLI never closes its one connection (the process exits), and tests
    follow suit; left to the garbage collector, each one raises a
    ResourceWarning in whichever later test happens to trigger the collection,
    which ``filterwarnings = error`` turns into a failure there.
    """
    opened: list[sqlite3.Connection] = []
    real_connect = db.connect

    def tracking_connect() -> sqlite3.Connection:
        conn = real_connect()
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "connect", tracking_connect)
    yield
    for conn in opened:
        conn.close()
