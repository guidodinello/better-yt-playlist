"""``serve``: a small web page for ``play``, made for a phone.

Search the songs playlist, tap a song (or its artist), get the queue and one
button that opens it on YouTube. Server-rendered HTML, no JavaScript. The
routes only parse the request and call the same functions as ``byp play``
(:func:`.play.seed_queue`, :func:`.play.artist_queue`). Read-only apart from
the on-the-spot Last.fm match ``resolve_seed`` does for a seed that has none
yet.

Seeds are always picked by video id from the song list, so a fuzzy query
never has to be resolved and :class:`.similar.AmbiguousSeed` can't happen.

Each request opens its own SQLite connection and Last.fm client: plain ``def``
routes run in FastAPI's thread pool, a connection can't cross threads, and the
client's rate limiter isn't thread-safe.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from html import escape
from typing import Annotated
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .lastfm import LastfmApi, LastfmError
from .play import WATCH_VIDEOS_MAX, QueueEntry, Reason, artist_queue, seed_queue, watch_url
from .search import search
from .similar import SeedError, YoutubeResolver, resolve_seed

DEFAULT_LIMIT = 25
LIMIT_CHOICES = (10, 25, WATCH_VIDEOS_MAX)
MAX_NEW = 10  # each new song is a yt-dlp search on first use; keep a tap from taking minutes
_IDENTIFIED = ("matched", "manual")


@dataclass(frozen=True, slots=True)
class Song:
    video_id: str
    title: str
    artist: str | None
    track: str | None

    @property
    def matched(self) -> bool:
        return self.artist is not None and self.track is not None


class BadRequest(ValueError):
    """A request the page can't serve; the message is shown to the user as-is."""


# --- data -------------------------------------------------------------------


def find_songs(conn: sqlite3.Connection, playlist_id: str, query: str) -> list[Song]:
    """Live, playable songs matching ``query`` (see :func:`.search.search`), best first.

    Searches the YouTube title, the Last.fm artist/track and the channel. An empty
    query lists every song in playlist order.
    """
    rows = conn.execute(
        """
        SELECT p.video_id, MIN(p.title) AS title, MIN(p.channel_title) AS channel,
               m.artist, m.track
        FROM playlist_items p
        LEFT JOIN track_metadata m
               ON m.video_id = p.video_id AND m.status IN (?, ?)
        WHERE p.playlist_id = ? AND p.removed_at IS NULL AND p.unavailable_at IS NULL
        GROUP BY p.video_id
        ORDER BY MIN(p.position)
        """,
        (*_IDENTIFIED, playlist_id),
    ).fetchall()
    found = search(rows, query, _searched_text)
    return [Song(r["video_id"], r["title"] or "", r["artist"], r["track"]) for r in found]


def _searched_text(row: sqlite3.Row) -> str:
    fields: tuple[str | None, ...] = (row["artist"], row["track"], row["title"], row["channel"])
    return " ".join(f for f in fields if f)


# --- app --------------------------------------------------------------------


def create_app(
    playlist_id: str,
    *,
    connect: Callable[[], sqlite3.Connection],
    lastfm: Callable[[], LastfmApi],
    resolver: YoutubeResolver | None,
) -> FastAPI:
    """The web app. ``resolver`` is None when yt-dlp isn't installed: no new songs then."""
    app = FastAPI(title="byp", docs_url=None, redoc_url=None, openapi_url=None)

    def search(q: str = "") -> str:
        with closing(connect()) as conn:
            return render_search(q, find_songs(conn, playlist_id, q))

    def play(
        v: str = "",
        artist: str = "",
        limit: Annotated[int, Query(ge=1, le=WATCH_VIDEOS_MAX)] = DEFAULT_LIMIT,
        shuffle: bool = False,
        new: Annotated[int, Query(ge=0, le=MAX_NEW)] = 0,
    ) -> str:
        v, artist = v.strip(), artist.strip()
        if bool(v) == bool(artist):
            raise BadRequest("Pick a song or an artist.")
        with closing(connect()) as conn:
            if artist:
                queue = artist_queue(conn, playlist_id, artist, limit=limit, shuffle=shuffle)
                if not queue:
                    raise BadRequest(f"No matched songs by {artist!r} in the playlist.")
                return render_queue(
                    artist, {"artist": artist}, queue, Options(limit, shuffle, None)
                )
            api = lastfm()
            seed = resolve_seed(conn, playlist_id, v, api=api)
            try:
                queue = seed_queue(
                    conn,
                    playlist_id,
                    seed,
                    api=api,
                    limit=limit,
                    new=new if resolver else 0,
                    resolver=resolver,
                    shuffle=shuffle,
                )
            except SystemExit as exc:  # library code exits on a missing tool; don't kill the server
                raise RuntimeError(str(exc)) from exc
        options = Options(limit, shuffle, new if resolver else None)
        return render_queue(seed.title or f"{seed.track} — {seed.artist}", {"v": v}, queue, options)

    async def bad_request(request: Request, exc: BadRequest) -> HTMLResponse:
        return HTMLResponse(render_error(str(exc)), status_code=400)

    async def invalid_params(request: Request, exc: RequestValidationError) -> HTMLResponse:
        problems = "; ".join(f"{e['loc'][-1]}: {e['msg']}" for e in exc.errors())
        return HTMLResponse(render_error(problems), status_code=400)

    async def unknown_seed(request: Request, exc: SeedError) -> HTMLResponse:
        return HTMLResponse(render_error(str(exc)), status_code=404)

    async def lastfm_failed(request: Request, exc: LastfmError) -> HTMLResponse:
        return HTMLResponse(render_error(f"Last.fm: {exc}"), status_code=502)

    async def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        return HTMLResponse(render_error(str(exc.detail)), status_code=exc.status_code)

    async def crashed(request: Request, exc: Exception) -> HTMLResponse:
        # Starlette re-raises after sending this, so uvicorn still logs the traceback.
        return HTMLResponse(render_error("Something broke — see the logs."), status_code=500)

    # Registered by call rather than decorator so pyright sees them used.
    app.get("/", response_class=HTMLResponse)(search)
    app.get("/play", response_class=HTMLResponse)(play)
    app.exception_handler(BadRequest)(bad_request)
    app.exception_handler(RequestValidationError)(invalid_params)
    app.exception_handler(SeedError)(unknown_seed)
    app.exception_handler(LastfmError)(lastfm_failed)
    app.exception_handler(StarletteHTTPException)(http_error)
    app.exception_handler(Exception)(crashed)
    return app


# --- rendering --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Options:
    limit: int
    shuffle: bool
    new: int | None  # None: new songs aren't offered (artist queue, or no yt-dlp)


_REASON_LABEL = {
    Reason.SEED: "seed",
    Reason.TRACK: "similar",
    Reason.ARTIST: "via artist",
    Reason.NEW: "new",
    Reason.BY_ARTIST: "",
}

_CSS = """
:root { color-scheme: light dark; --fg: #1a1a1a; --muted: #6b6b6b; --bg: #fafafa;
  --line: #e2e2e2; --accent: #c4302b; --chip: #ececec; }
@media (prefers-color-scheme: dark) { :root { --fg: #ececec; --muted: #9a9a9a;
  --bg: #121212; --line: #2a2a2a; --accent: #ff4e45; --chip: #262626; } }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 40rem; padding: 1rem; background: var(--bg);
  color: var(--fg); font: 16px/1.4 system-ui, sans-serif; }
a { color: inherit; }
h1 { font-size: 1.25rem; margin: .25rem 0 1rem; }
form.search { display: flex; gap: .5rem; margin-bottom: 1rem; }
input[type=search] { flex: 1; font: inherit; padding: .6rem .75rem; border-radius: .5rem;
  border: 1px solid var(--line); background: transparent; color: inherit; min-width: 0; }
button, select { font: inherit; padding: .6rem .9rem; border-radius: .5rem;
  border: 1px solid var(--line); background: var(--chip); color: inherit; }
ul, ol { list-style: none; margin: 0; padding: 0; }
li { padding: .6rem 0; border-bottom: 1px solid var(--line); }
li .song { display: block; text-decoration: none; font-weight: 500; }
li .meta { color: var(--muted); font-size: .9rem; }
.chip { display: inline-block; font-size: .75rem; padding: .05rem .45rem; border-radius: 1rem;
  background: var(--chip); color: var(--muted); margin-left: .35rem; }
.play { display: block; text-align: center; text-decoration: none; font-weight: 600;
  font-size: 1.15rem; padding: .9rem; border-radius: .75rem; background: var(--accent);
  color: #fff; margin-bottom: 1rem; }
form.options { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center;
  margin-bottom: 1rem; color: var(--muted); }
.muted { color: var(--muted); font-size: .9rem; }
.back { display: inline-block; margin-bottom: .5rem; color: var(--muted); }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)} · byp</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def _href(path: str, params: dict[str, str]) -> str:
    return escape(f"{path}?{urlencode(params)}")


def render_search(query: str, songs: Sequence[Song]) -> str:
    items: list[str] = []
    for s in songs:
        # The upload's own title: it says which version this is (a remix, an edit,
        # slowed + reverb), which the Last.fm name hides.
        song_link = (
            f'<a class=song href="{_href("/play", {"v": s.video_id})}">{escape(s.title)}</a>'
        )
        if s.matched:
            artist_link = _href("/play", {"artist": s.artist or ""})
            meta = f'<a href="{artist_link}">{escape(s.artist or "")}</a>'
        else:
            meta = "not matched to Last.fm yet<span class=chip>?</span>"
        items.append(f"<li>{song_link}<span class=meta>{meta}</span></li>")
    count = f"{len(songs)} song{'s' if len(songs) != 1 else ''}"
    body = (
        "<h1>Play from the playlist</h1>"
        '<form class=search action="/" method=get>'
        f'<input type=search name=q value="{escape(query)}" placeholder="Song or artist"'
        ' aria-label="Song or artist">'
        "<button>Search</button></form>"
        f"<p class=muted>{count} — tap a song for it and similar ones, "
        "or an artist for their songs.</p>"
        f"<ul>{''.join(items)}</ul>"
    )
    return _page("Search", body)


def render_queue(
    title: str, target: dict[str, str], queue: Sequence[QueueEntry], options: Options
) -> str:
    items: list[str] = []
    for e in queue:
        label = _REASON_LABEL[e.reason]
        chip = f"<span class=chip>{escape(label)}</span>" if label else ""
        items.append(
            f'<li><a class=song href="https://youtu.be/{escape(e.video_id)}">'
            f"{escape(e.title or e.track)}</a><span class=meta>{escape(e.artist)}{chip}</span></li>"
        )
    url = watch_url([e.video_id for e in queue])
    body = (
        '<a class=back href="/">← Search</a>'
        f"<h1>{escape(title)}</h1>"
        f'<a class=play href="{escape(url)}">▶ Play {len(queue)} songs</a>'
        f"{_options_form(target, options)}"
        f"<ol>{''.join(items)}</ol>"
    )
    if any(e.reason is Reason.ARTIST for e in queue):
        body += (
            "<p class=muted>via artist: Last.fm has no track-level data here; "
            "picked by similar artists.</p>"
        )
    return _page(title, body)


def _options_form(target: dict[str, str], options: Options) -> str:
    hidden = "".join(
        f'<input type=hidden name="{escape(k)}" value="{escape(v)}">' for k, v in target.items()
    )
    limits = "".join(
        f"<option value={n}{' selected' if n == options.limit else ''}>{n} songs</option>"
        for n in sorted({*LIMIT_CHOICES, options.limit})
    )
    shuffle = " checked" if options.shuffle else ""
    new = ""
    if options.new is not None:
        new = (
            f'<label>+ <input type=number name=new min=0 max={MAX_NEW} value="{options.new}"'
            ' style="width:3.5rem;font:inherit"> new</label>'
        )
    return (
        "<form class=options action=/play method=get>"
        f"{hidden}<select name=limit aria-label=Length>{limits}</select>"
        f"<label><input type=checkbox name=shuffle value=1{shuffle}> shuffle</label>"
        f"{new}<button>Update</button></form>"
    )


def render_error(message: str) -> str:
    return _page("Oops", f'<a class=back href="/">← Search</a><h1>{escape(message)}</h1>')


# --- server -----------------------------------------------------------------


def run(app: FastAPI, *, host: str, port: int) -> None:
    """Serve until interrupted."""
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
