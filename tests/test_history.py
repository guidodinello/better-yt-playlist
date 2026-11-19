"""import-history: the watch history feed is stored, deduplicated, and only accumulates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from better_yt_playlist import db
from better_yt_playlist.history import import_history, parse_entries, ytdlp_history_cmd


def _line(video_id: str, title: str = "", channel: str = "Chan", duration: Any = 60) -> str:
    return json.dumps(
        {
            "id": video_id,
            "title": title or f"title-{video_id}",
            "channel": channel,
            "channel_id": f"UC{channel}",
            "duration": duration,
        }
    )


def _rows(conn: Any) -> dict[str, Any]:
    return {r["video_id"]: r for r in conn.execute("SELECT * FROM watch_history")}


def test_reads_the_history_feed_with_browser_cookies() -> None:
    cmd = ytdlp_history_cmd("firefox")
    assert cmd[-1] == ":ythistory"
    assert cmd[cmd.index("--cookies-from-browser") + 1] == "firefox"
    assert "--playlist-end" in cmd  # without it yt-dlp printed nothing


def test_rewatched_video_keeps_its_most_recent_place() -> None:
    lines = [_line("b"), "", "not json", _line("a"), _line("b"), _line("c", duration=None)]
    entries = parse_entries(lines)
    assert [e["video_id"] for e in entries] == ["b", "a", "c"]
    assert entries[2]["duration_s"] is None


def test_import_from_file_then_reimport_accumulates(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "h.db"))
    conn = db.connect()
    first = tmp_path / "first.jsonl"
    first.write_text("\n".join([_line("new1"), _line("old1"), _line("old2")]))

    stats = import_history(file=first, conn=conn)
    assert stats == {"videos": 3, "new": 3, "total": 3}
    rows = _rows(conn)
    assert rows["new1"]["position"] == 0
    assert rows["old2"]["position"] == 2
    assert rows["new1"]["channel_title"] == "Chan"

    # Later read: one new watch, old2 aged out of the feed, old1 renamed.
    second = tmp_path / "second.jsonl"
    second.write_text("\n".join([_line("new2"), _line("new1"), _line("old1", "renamed")]))
    stats = import_history(file=second, conn=conn)
    assert stats == {"videos": 3, "new": 1, "total": 4}
    rows = _rows(conn)
    assert rows["old2"]["position"] is None  # kept, but no longer in the feed
    assert rows["new2"]["position"] == 0
    assert rows["old1"]["title"] == "renamed"
    assert rows["old1"]["first_seen_at"] <= rows["old1"]["last_seen_at"]


def test_empty_read_changes_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "e.db"))
    conn = db.connect()
    good = tmp_path / "good.jsonl"
    good.write_text(_line("a"))
    import_history(file=good, conn=conn)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(SystemExit):
        import_history(file=empty, conn=conn)
    assert _rows(conn)["a"]["position"] == 0


def test_failed_ytdlp_run_stores_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("BYP_DB", str(tmp_path / "f.db"))
    conn = db.connect()

    class _Result:
        returncode = 1
        stdout = _line("partial")
        stderr = "ERROR: cookies"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Result())
    with pytest.raises(SystemExit):
        import_history(browser="firefox", conn=conn)
    assert _rows(conn) == {}
