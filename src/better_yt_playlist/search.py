"""Forgiving song search: any word order, accents and punctuation ignored, typos tolerated.

A query matches a song when **every** query word matches some word of the
song's text. A word matches exactly, as a prefix (``sick`` → SICKO), or — for
words of :data:`FUZZY_MIN_LEN`+ letters — fuzzily (``goosbumps`` → goosebumps),
scored in that order so the best hits rank first.

Fuzzy matching runs once per query word against the *vocabulary* (the distinct
words of all songs, a few thousand), not per song, so plain ``difflib`` is fast
enough for a playlist of hundreds of songs.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass

FUZZY_MIN_LEN = 4  # shorter words fuzz into too many others ("on" ~ "one", "no")
FUZZY_CUTOFF = 0.8  # difflib ratio: "goosbumps"/"goosebumps" 0.95, "scot"/"scott" 0.89
PREFIX_MIN_LEN = 2

_EXACT, _PREFIX, _FUZZY = 3, 2, 1
_PHRASE_BONUS = 1  # the whole query appears as-is: "la llaman salvaje" over a scattered match
_NON_WORD = re.compile(r"[^\w]+|_")


def normalize(text: str) -> str:
    """Lowercase, accents and punctuation stripped, words single-spaced.

    ``"ROSALÍA - DESPECHÁ"`` → ``"rosalia despecha"``, ``"FE!N"`` → ``"fe n"``…
    so punctuation inside a word is also tried glued: see :func:`words`.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_NON_WORD.sub(" ", plain).split())


def words(text: str) -> set[str]:
    """The normalized words of ``text``, plus each punctuated word glued (``FE!N`` → fe, n, fen)."""
    out = set(normalize(text).split())
    for raw in text.split():
        glued = normalize(raw).replace(" ", "")
        if glued:
            out.add(glued)
    return out


@dataclass(frozen=True, slots=True)
class _Doc[T]:
    item: T
    words: frozenset[str]
    phrase: str  # normalized full text, for the phrase bonus


def search[T](items: Sequence[T], query: str, text: Callable[[T], str]) -> list[T]:
    """``items`` matching every word of ``query``, best first; ties keep ``items``' order.

    An empty query returns all items unchanged.
    """
    if not normalize(query):
        return list(items)
    return [item for item, _ in rank(items, query, text)]


def rank[T](items: Sequence[T], query: str, text: Callable[[T], str]) -> list[tuple[T, int]]:
    """Like :func:`search`, with each match's score (higher is better); empty query, no matches."""
    query_words = normalize(query).split()
    if not query_words:
        return []
    docs = [_Doc(item, frozenset(words(text(item))), normalize(text(item))) for item in items]
    vocabulary = sorted({w for d in docs for w in d.words})
    scores_per_word = [_word_scores(q, vocabulary) for q in query_words]
    phrase = " ".join(query_words)

    ranked: list[tuple[int, int, T]] = []
    for index, doc in enumerate(docs):
        total = 0
        for scores in scores_per_word:
            best = max((scores.get(w, 0) for w in doc.words), default=0)
            if not best:
                break  # every query word must match
            total += best
        else:
            if len(query_words) > 1 and phrase in doc.phrase:
                total += _PHRASE_BONUS
            ranked.append((-total, index, doc.item))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [(item, -neg_score) for neg_score, _, item in ranked]


def _word_scores(query_word: str, vocabulary: Sequence[str]) -> dict[str, int]:
    """How well each vocabulary word matches ``query_word``: exact > prefix > fuzzy."""
    scores: dict[str, int] = {}
    if len(query_word) >= FUZZY_MIN_LEN:
        for w in difflib.get_close_matches(
            query_word, vocabulary, n=len(vocabulary), cutoff=FUZZY_CUTOFF
        ):
            scores[w] = _FUZZY
    if len(query_word) >= PREFIX_MIN_LEN:
        for w in vocabulary:
            if w.startswith(query_word):
                scores[w] = _PREFIX
    if query_word in scores or query_word in vocabulary:  # in scores: found as its own prefix
        scores[query_word] = _EXACT
    return scores
