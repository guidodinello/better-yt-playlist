"""Turn a YouTube music-video title into an (artist, track) guess.

No network: these are best-effort heuristics whose output ``match`` then
confirms (or rejects) against Last.fm. Two separate jobs live here:

- :func:`parse_title` — read artist/track out of titles like
  ``"Tame Impala - Loser (Official Video)"`` or a bare ``"La Vecina"`` posted by
  an ``"Amar Azul - Topic"`` channel.
- :func:`norm` — a comparison key, applied to **both** sides whenever two
  names are compared (playlist vs Last.fm), so ``"Umbrella (Orange Version)
  ft. JAY-Z"`` and ``"umbrella"`` meet. Stored names keep their display form.
"""

from __future__ import annotations

import re
import unicodedata

# A bracketed chunk is noise if it mentions any of these (case-insensitive).
_NOISE_WORDS = (
    "official",
    "oficial",
    "video",
    "audio",
    "lyric",
    "letra",
    "visuali",
    "explicit",
    "album version",
    "hd",
    "4k",
    "hq",
    "remaster",
    "full",
    "on screen",
    "prod",
)
_NOISE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _NOISE_WORDS) + r")",
    re.IGNORECASE,
)
_BRACKETS_RE = re.compile(r"\s*[(\[][^()\[\]]*[)\]]")
_NESTED_BRACKETS_RE = re.compile(r"\s*\([^()]*\([^()]*\)[^()]*\)")
# "Artist - Track", "Artist – Track", "Artist -Track"; never the hyphen in "Wu-Tang".
_DASH_RE = re.compile(r"\s+[-‒–—―]+\s*|\s*[-‒–—―]+\s+")
# "ft. X", "feat X", "Ft.X" — but not "ft" inside a word ("Loft").
_FEAT_RE = re.compile(r"\s*[(\[]?\b(?:feat|ft|featuring)\b(?:\.\s*|\s+).*$", re.IGNORECASE)
_LEADING_QUOTE_RE = re.compile(r'^["“]([^"“”]+)["”]')
# A quoted lyric line tacked on after the track: 'Daylight "oh i love it..."'.
_TRAILING_QUOTE_RE = re.compile(r'\s+["“][^"“”]*["”]\s*$')
_QUOTED_RE = re.compile(r'^(?P<artist>[^"“”]+?)\s*["“](?P<track>[^"“”]+)["”]')
_TOPIC_SUFFIX = " - Topic"


def _is_noise(chunk: str) -> bool:
    return bool(_NOISE_RE.search(chunk))


def _strip_noise_brackets(text: str) -> str:
    text = _NESTED_BRACKETS_RE.sub(lambda m: "" if _is_noise(m.group()) else m.group(), text)
    return _BRACKETS_RE.sub(lambda m: "" if _is_noise(m.group()) else m.group(), text).strip()


def _strip_noise_segments(text: str) -> str:
    """Drop trailing ``- Lyrics - HD`` / ``| Full HD`` style segments."""
    parts = re.split(r"\s+[-–—|]\s+", text)
    while len(parts) > 1 and _is_noise(parts[-1]):
        parts.pop()
    return " - ".join(parts)


def clean_title(title: str) -> str:
    """The title with video noise removed — what to hand ``track.search``."""
    return _strip_noise_segments(_strip_noise_brackets(title))


def _clean_track(track: str) -> str:
    track = clean_title(track)
    leading = _LEADING_QUOTE_RE.match(track)
    if leading:
        track = leading.group(1)
    track = _TRAILING_QUOTE_RE.sub("", track)
    track = _FEAT_RE.sub("", track)
    return track.strip(" \"'“”")


def _clean_artist(artist: str) -> str:
    return _FEAT_RE.sub("", artist).strip(" \"'“”")


def parse_title(title: str, channel_title: str | None) -> tuple[str, str] | None:
    """Best-effort (artist, track) from a video title, or ``None`` if no shape fits."""
    text = _strip_noise_brackets(title)

    if parsed := _split(text, _DASH_RE):
        return parsed

    quoted = _QUOTED_RE.match(text)
    if quoted:
        return _clean_artist(quoted["artist"]), _clean_track(quoted["track"])

    for splitter in (re.compile(r"\s+\|\s+"), re.compile(r":\s+")):
        if parsed := _split(text, splitter):
            return parsed

    if channel_title and channel_title.endswith(_TOPIC_SUFFIX):
        track = _clean_track(text)
        if track:
            return channel_title.removesuffix(_TOPIC_SUFFIX).strip(), track
    return None


def _split(text: str, splitter: re.Pattern[str]) -> tuple[str, str] | None:
    parts = splitter.split(text, maxsplit=1)
    if len(parts) != 2:
        return None
    artist, track = _clean_artist(parts[0]), _clean_track(parts[1])
    return (artist, track) if artist and track else None


def artist_candidates(artist: str) -> list[str]:
    """The credited artist, then its first-listed name for multi-artist credits.

    ``"Bad Bunny x Jory Boy"`` → ``["Bad Bunny x Jory Boy", "Bad Bunny"]``. The
    full credit is tried first because some acts are named with ``&``
    (``"Wisin & Yandel"``).
    """
    first = re.split(r"\s+(?:x|X|&|and|y|vs\.?)\s+|,\s*", artist, maxsplit=1)[0].strip()
    candidates = [artist]
    # "The Ramones" is "Ramones" on Last.fm.
    for variant in (
        first,
        re.sub(r"^the\s+", "", artist, flags=re.IGNORECASE),
        _BRACKETS_RE.sub("", artist).strip(),
    ):
        if variant and variant not in candidates:
            candidates.append(variant)
    return candidates


def track_candidates(track: str) -> list[str]:
    """The track, then without bracketed qualifiers, then its first ``-`` segment.

    The last covers ``"Artist | Track | Album"`` titles, which parse to
    ``"Track - Album"``.
    """
    candidates = [track]
    for variant in (_BRACKETS_RE.sub("", track), re.split(r"\s+-\s+", track, maxsplit=1)[0]):
        variant = variant.strip()
        if variant and variant not in candidates:
            candidates.append(variant)
    return candidates


def words_within(phrase: str, text: str) -> bool:
    """Whether every word of ``phrase`` (normalized) appears in ``text``."""
    wanted = set(_norm_part(phrase).split())
    return bool(wanted) and wanted <= set(_norm_part(text).split())


def norm(artist: str, track: str) -> tuple[str, str]:
    """Comparison key: casefolded, accent-free, no feat./brackets/version suffixes."""
    return _norm_part(_clean_artist(artist)), _norm_part(_norm_track(track))


def _norm_track(track: str) -> str:
    track = _FEAT_RE.sub("", track)
    track = _BRACKETS_RE.sub("", track)
    # "Song - Remastered 2011", "Song - Live", "Song - Radio Edit"
    return re.split(r"\s+[-–—]\s+", track, maxsplit=1)[0]


def _norm_part(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", no_marks).split())
