"""Hand-written fakes for the package's outside dependencies.

Each fake satisfies its Protocol structurally; the module-level ``_*_conforms``
assignments make pyright flag any drift between a fake and its Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from better_yt_playlist.lastfm import LastfmApi, SimilarArtist, SimilarTrack, TrackInfo
from better_yt_playlist.similar import YoutubeHit, YoutubeResolver
from better_yt_playlist.titles import norm


@dataclass(slots=True)
class FakeLastfm:
    """Satisfies :class:`LastfmApi`. Lookups go through ``norm`` like Last.fm's autocorrect."""

    known: list[TrackInfo] = field(default_factory=list[TrackInfo])
    search_results: dict[str, list[TrackInfo]] = field(default_factory=dict[str, list[TrackInfo]])
    similar: dict[tuple[str, str], list[SimilarTrack]] = field(
        default_factory=dict[tuple[str, str], list[SimilarTrack]]
    )
    similar_artist_map: dict[str, list[SimilarArtist]] = field(
        default_factory=dict[str, list[SimilarArtist]]
    )
    top: dict[str, list[TrackInfo]] = field(default_factory=dict[str, list[TrackInfo]])
    info_calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    similar_calls: int = 0
    artist_calls: int = 0

    def track_info(self, artist: str, track: str) -> TrackInfo | None:
        self.info_calls.append((artist, track))
        key = norm(artist, track)
        return next((t for t in self.known if norm(t.artist, t.track) == key), None)

    def search_track(self, query: str, *, limit: int = 5) -> list[TrackInfo]:
        return self.search_results.get(query, [])[:limit]

    def similar_tracks(self, artist: str, track: str, *, limit: int) -> list[SimilarTrack]:
        self.similar_calls += 1
        return self.similar.get(norm(artist, track), [])[:limit]

    def similar_artists(self, artist: str, *, limit: int) -> list[SimilarArtist]:
        self.artist_calls += 1
        return self.similar_artist_map.get(norm(artist, "")[0], [])[:limit]

    def top_tracks(self, artist: str, *, limit: int) -> list[TrackInfo]:
        self.artist_calls += 1
        return self.top.get(norm(artist, "")[0], [])[:limit]


@dataclass(slots=True)
class FakeResolver:
    """Satisfies :class:`YoutubeResolver` from a fixed ``"artist - track" -> hit`` map."""

    hits: dict[str, YoutubeHit] = field(default_factory=dict[str, YoutubeHit])
    calls: list[str] = field(default_factory=list[str])

    def resolve(self, artist: str, track: str) -> YoutubeHit | None:
        query = f"{artist} - {track}"
        self.calls.append(query)
        return self.hits.get(query)


_lastfm_conforms: LastfmApi = FakeLastfm()
_resolver_conforms: YoutubeResolver = FakeResolver()
