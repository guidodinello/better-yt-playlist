"""``play``: turn a seed song (or an artist) into a YouTube watch queue.

The queue is a ``watch_videos`` link: YouTube expands it into a temporary,
unsaved playlist on open. No login, no API quota — but the endpoint is
undocumented and silently drops every id past the 50th (checked live), so
queues are capped at :data:`WATCH_VIDEOS_MAX` and the per-song links are
printed too as a fallback.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .similar import Recommendation, Seed

WATCH_VIDEOS_MAX = 50
_WATCH_VIDEOS_URL = "https://www.youtube.com/watch_videos?video_ids="


class Reason(StrEnum):
    SEED = "seed"
    TRACK = "track"  # Last.fm track similarity, song already in the playlist
    ARTIST = "artist"  # similar-artist fallback, song already in the playlist
    NEW = "new"  # from discover: not in the playlist yet
    BY_ARTIST = "by artist"  # `play --artist`


@dataclass(frozen=True, slots=True)
class QueueEntry:
    video_id: str
    artist: str
    track: str
    reason: Reason


def seed_entry(seed: Seed) -> QueueEntry:
    return QueueEntry(seed.video_id, seed.artist, seed.track, Reason.SEED)


def from_similar(recs: Sequence[Recommendation]) -> list[QueueEntry]:
    """``similar`` results, tagged by how Last.fm found them (track or artist)."""
    return [QueueEntry(r.video_id, r.artist, r.track, Reason(r.basis)) for r in recs if r.video_id]


def tagged(recs: Sequence[Recommendation], reason: Reason) -> list[QueueEntry]:
    return [QueueEntry(r.video_id, r.artist, r.track, reason) for r in recs if r.video_id]


def build_queue(
    head: Sequence[QueueEntry],
    main: Sequence[QueueEntry],
    extra: Sequence[QueueEntry] = (),
    *,
    limit: int,
) -> list[QueueEntry]:
    """``head`` first, then ``main`` with ``extra`` spread evenly through it.

    At most ``limit`` entries (and never more than WATCH_VIDEOS_MAX), each
    video once. ``extra`` keeps all its entries that fit; ``main`` is trimmed
    from the tail — it's ordered best first.
    """
    limit = min(limit, WATCH_VIDEOS_MAX)
    seen: set[str] = set()

    def fresh(entries: Sequence[QueueEntry]) -> list[QueueEntry]:
        out: list[QueueEntry] = []
        for e in entries:
            if e.video_id not in seen:
                seen.add(e.video_id)
                out.append(e)
        return out

    first = fresh(head)[:limit]
    room = limit - len(first)
    rest_extra = fresh(extra)[:room]
    rest_main = fresh(main)[: room - len(rest_extra)]
    return first + _interleave(rest_main, rest_extra)


def _interleave(main: list[QueueEntry], extra: list[QueueEntry]) -> list[QueueEntry]:
    """``extra`` spread evenly through ``main``, both keeping their order."""
    out = list(main)
    # Rounded up, so a short ``main`` still leads with its best entry. Inserted
    # back to front so the earlier insertion points stay valid.
    for i in reversed(range(len(extra))):
        out.insert(-(-(i + 1) * len(main) // (len(extra) + 1)), extra[i])
    return out


def watch_url(video_ids: Sequence[str]) -> str:
    """One link that plays ``video_ids`` in order as a temporary playlist."""
    if not video_ids:
        raise ValueError("no videos to play")
    if len(video_ids) > WATCH_VIDEOS_MAX:
        raise ValueError(f"watch_videos takes at most {WATCH_VIDEOS_MAX} ids")
    return _WATCH_VIDEOS_URL + ",".join(video_ids)
