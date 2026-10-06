"""titles: (artist, track) parsing from video titles, and the comparison key."""

from __future__ import annotations

import pytest

from better_yt_playlist.titles import (
    artist_candidates,
    clean_title,
    norm,
    parse_title,
    track_candidates,
    words_within,
)


@pytest.mark.parametrize(
    ("title", "channel", "expected"),
    [
        ("Tame Impala - Loser (Official Video)", "tameimpalaVEVO", ("Tame Impala", "Loser")),
        ("Bad Bunny x Jory Boy -  No Te Hagas ( Video Oficial )", "Hear This Music",
         ("Bad Bunny x Jory Boy", "No Te Hagas")),
        ("Katy Perry - Last Friday Night (T.G.I.F.) (Official Music Video)", "KatyPerryVEVO",
         ("Katy Perry", "Last Friday Night (T.G.I.F.)")),
        ("Logic1000 – What You Like (feat. Yunè Pinku) (Official Visualiser)", "Logic1000",
         ("Logic1000", "What You Like")),
        ("Central Cee - BAND4BAND (Lyrics) Ft. Lil Baby", "Vibe Music",
         ("Central Cee", "BAND4BAND")),
        ("Eminem - White America - Lyrics - HD", "x", ("Eminem", "White America")),
        ("Eminem - 97' Bonnie And Clyde | Lyrics on screen | Full HD", "x",
         ("Eminem", "97' Bonnie And Clyde")),
        ("Mobb Deep - Survival of the Fittest (Official Video) [Explicit]", "UPROXX",
         ("Mobb Deep", "Survival of the Fittest")),
        ("Wu-Tang Clan - Bring Da Ruckus (Official Audio)", "x",
         ("Wu-Tang Clan", "Bring Da Ruckus")),
        ("Kali Uchis -Telepatia (sped up+reverb)", "x",
         ("Kali Uchis", "Telepatia (sped up+reverb)")),
        ("La Vela Puerca | Zafar | A Contraluz", "x", ("La Vela Puerca", "Zafar - A Contraluz")),
        ("twenty one pilots: Stressed Out [OFFICIAL VIDEO]", "Fueled By Ramen",
         ("twenty one pilots", "Stressed Out")),
        ('Dominic Fike "Babydoll" (Official Audio)', "Dominic Fike", ("Dominic Fike", "Babydoll")),
        ('Re Fantasma ft. Mala Fama "Yo Uso Visera" (Video Oficial)', "x",
         ("Re Fantasma", "Yo Uso Visera")),
        # Shapes that sent real titles to the (error-prone) search fallback.
        ('David Kushner - Daylight (Lyrics) "oh i love it and i hate it at the same time"', "x",
         ("David Kushner", "Daylight")),
        ("Shawn Mendes ‒ There's Nothing Holding Me Back (Lyrics)", "x",
         ("Shawn Mendes", "There's Nothing Holding Me Back")),
        ("Eloy  Ft.Randy - Fuera Del Planeta", "x", ("Eloy", "Fuera Del Planeta")),
        ('Flaco Vazquez - "Paralizado" (Prod. by Brujo)', "x", ("Flaco Vazquez", "Paralizado")),
        # No separator: a "<Artist> - Topic" channel supplies the artist.
        ("La Vecina", "Amar Azul - Topic", ("Amar Azul", "La Vecina")),
        ("When It Rains It Pours (Album Version (Explicit))", "50 Cent - Topic",
         ("50 Cent", "When It Rains It Pours")),
        # Nothing to go on.
        ("MONTAGEM BAILÃO", "TheGoodVibe", None),
        ("GTA San Andreas Theme Song Full ! !", "Buttonik", None),
        ("La Vecina", None, None),
    ],
)  # fmt: skip
def test_parse_title(title: str, channel: str | None, expected: tuple[str, str] | None) -> None:
    assert parse_title(title, channel) == expected


def test_clean_title_drops_video_noise_only() -> None:
    assert clean_title("MIRANDA! Fantasmas (Video Oficial)") == "MIRANDA! Fantasmas"
    assert clean_title("Umbrella (Orange Version)") == "Umbrella (Orange Version)"


def test_artist_candidates_fall_back_to_first_credited() -> None:
    assert artist_candidates("Bad Bunny x Jory Boy") == ["Bad Bunny x Jory Boy", "Bad Bunny"]
    assert artist_candidates("Wisin & Yandel") == ["Wisin & Yandel", "Wisin"]
    assert artist_candidates("DENNIS, Luísa Sonza, Emilia") == [
        "DENNIS, Luísa Sonza, Emilia",
        "DENNIS",
    ]
    assert artist_candidates("Adele") == ["Adele"]
    assert artist_candidates("The Ramones") == ["The Ramones", "Ramones"]


def test_words_within() -> None:
    assert words_within("Que Dolor", "La Nueva Escuela - Qué Dolor (Video Oficial)")
    assert not words_within("The Bitch Is Back", "The Vamp Is Back")
    assert not words_within("", "anything")


def test_track_candidates() -> None:
    assert track_candidates("Umbrella (Orange Version)") == [
        "Umbrella (Orange Version)",
        "Umbrella",
    ]
    assert track_candidates("Zafar - A Contraluz") == ["Zafar - A Contraluz", "Zafar"]
    assert track_candidates("Loser") == ["Loser"]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (("Rihanna", "Umbrella (Orange Version) ft. JAY-Z"), ("rihanna", "Umbrella")),
        (("ROSALÍA", "DESPECHÁ"), ("Rosalia", "Despecha")),
        (("The Beatles", "Let It Be - Remastered 2009"), ("the beatles", "let it be")),
        (("Daft Punk", "One More Time - Radio Edit"), ("Daft Punk", "One More Time")),
        (("Logic1000 feat. Yunè Pinku", "What You Like"), ("Logic1000", "What You Like")),
    ],
)
def test_norm_equates_name_variants(a: tuple[str, str], b: tuple[str, str]) -> None:
    assert norm(*a) == norm(*b)


def test_norm_keeps_different_songs_apart() -> None:
    assert norm("Tame Impala", "Loser") != norm("Tame Impala", "Lost in Yesterday")
