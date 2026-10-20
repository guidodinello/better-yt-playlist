"""search: normalization, any-order words, prefixes, typos, and ranking."""

from __future__ import annotations

import pytest

from better_yt_playlist.search import normalize, search, words

SONGS = [
    "Don Omar Salvaje LA LLAMAN SALVAJE (Turreo Edit) Ft Don Omar - REMIX",
    "Travis Scott goosebumps",
    "Travis Scott FE!N",
    "Travis Scott SICKO MODE",
    "ROSALÍA DESPECHÁ",
    "Busta Rhymes I Know What You Want",
    "Travis Scott I KNOW ?",
    "Tame Impala Loser",
]


def _find(query: str) -> list[str]:
    return search(SONGS, query, lambda s: s)


def test_normalize_folds_case_accents_and_punctuation() -> None:
    assert normalize("  ROSALÍA - DESPECHÁ!! ") == "rosalia despecha"


def test_words_also_glue_a_punctuated_word() -> None:
    assert words("FE!N") == {"fe", "n", "fen"}


@pytest.mark.parametrize("query", ["salvaje remix", "remix salvaje", "la llaman salvaje - remix"])
def test_words_match_in_any_order_and_punctuation_is_ignored(query: str) -> None:
    assert _find(query) == [SONGS[0]]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("despecha", SONGS[4]),  # accents
        ("ROSALIA", SONGS[4]),  # case, beyond ASCII
        ("fein", SONGS[2]),  # punctuation inside a word
        ("sick", SONGS[3]),  # prefix
        ("goosbumps", SONGS[1]),  # typo
    ],
)
def test_forgiving_matches(query: str, expected: str) -> None:
    assert _find(query) == [expected]


def test_every_word_must_match() -> None:
    assert _find("travis loser") == []


def test_typos_need_a_long_enough_word() -> None:
    assert _find("lsoer") == [SONGS[7]]  # swapped letters, 5 letters long
    assert _find("tme") == []  # 3 letters: neither a prefix of "tame" nor fuzzy


def test_exact_beats_prefix_beats_fuzzy_then_playlist_order() -> None:
    items = ["Inpala", "Impalas Live", "Impala", "Tame Impala"]
    assert search(items, "impala", lambda s: s) == [
        "Impala",
        "Tame Impala",
        "Impalas Live",
        "Inpala",
    ]


def test_whole_phrase_ranks_above_scattered_words() -> None:
    assert search(["know I", "I Know"], "i know", lambda s: s) == ["I Know", "know I"]


def test_empty_query_returns_everything_in_order() -> None:
    assert _find("   ") == SONGS
