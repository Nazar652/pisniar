"""`matching.is_match()`: normalization and the calibrated duration tolerance.
Every artist/title/duration below is a real value recorded live from Spotify,
Deezer, iTunes, ytmusicapi and song.link on nine reference tracks.
"""

from __future__ import annotations

import pytest

from pisniar.resolve.matching import (
    DURATION_TOLERANCE_MS,
    find_matching_candidate,
    is_match,
    is_valid_isrc,
    is_valid_spotify_id,
    is_valid_youtube_video_id,
    known_duration,
    normalize_artist_set,
    normalize_text,
    rejected_candidates_warning,
)
from pisniar.resolve.types import TrackCandidate


def test_duration_tolerance_is_three_seconds() -> None:
    assert DURATION_TOLERANCE_MS == 3000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("But Wait... (Official Video)", "but wait"),
        ("Berserker - Topic", "berserker"),
        ("Покаяння", "покаяння"),
        ("Покаяння (Official Audio)", "покаяння"),
        ("Song feat. Someone Else", "song"),
        ("Song ft. Someone", "song"),
    ],
)
def test_normalize_text_strips_noise(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_normalize_text_strips_diacritics() -> None:
    assert normalize_text("Alternativă") == normalize_text("Alternativa")


# spotify reference (artist, title, ms) vs the candidate the named source
# actually returned for it. Nine tracks, both alphabets, two sources.
REAL_CROSS_PLATFORM_PAIRS = [
    ("itunes", "АННА", "Горн", 270981, "Анна", "Горн", 270982),
    ("ytmusic", "АННА", "Горн", 270981, "Анна", "Горн", 271000),
    ("ytmusic", "Тол", "Сумніви", 293133, "ТОЛ", "Сумніви", 294000),
    (
        "itunes",
        "Burned Time Machine, 0%Mercury",
        "Покаяння",
        224124,
        "Burned Time Machine & 0%Mercury",
        "Покаяння",
        224125,
    ),
    (
        "ytmusic",
        "Burned Time Machine, 0%Mercury",
        "Покаяння",
        224124,
        "Burned Time Machine, 0%Mercury",
        "Покаяння",
        225000,
    ),
    ("itunes", "hackedface", "but wait...", 264000, "Hackedface", "But Wait...", 264000),
    ("ytmusic", "hackedface", "but wait...", 264000, "hackedface", "but wait...", 264000),
    (
        "ytmusic",
        "Sick Solution",
        "Церемонія (Прощання)",
        148774,
        "Sick Solution",
        "Церемонія (Прощання)",
        149000,
    ),
    ("itunes", "Krovna", "Berserker", 262105, "Krovna", "Berserker", 262105),
    ("ytmusic", "Krovna", "Berserker", 262105, "Krovna", "Berserker", 263000),
    ("itunes", "ПРОЗАК", "Сила", 309035, "ПРОЗАК", "Сила", 309035),
    ("ytmusic", "ПРОЗАК", "Сила", 309035, "ПРОЗАК", "Сила", 310000),
    (
        "ytmusic",
        "hackedface",
        "live like a saint/die like a sinner",
        157058,
        "hackedface",
        "live like a saint/die like a sinner",
        158000,
    ),
    ("ytmusic", "Sick Solution", "Зашморг", 160095, "Sick Solution", "Зашморг", 161000),
]


@pytest.mark.parametrize(
    (
        "source",
        "reference_artist",
        "reference_title",
        "reference_duration_ms",
        "candidate_artist",
        "candidate_title",
        "candidate_duration_ms",
    ),
    REAL_CROSS_PLATFORM_PAIRS,
)
def test_is_match_accepts_every_real_cross_platform_pair(
    source: str,
    reference_artist: str,
    reference_title: str,
    reference_duration_ms: int,
    candidate_artist: str,
    candidate_title: str,
    candidate_duration_ms: int,
) -> None:
    assert is_match(
        reference_artist=reference_artist,
        reference_title=reference_title,
        reference_duration_ms=reference_duration_ms,
        candidate_artist=candidate_artist,
        candidate_title=candidate_title,
        candidate_duration_ms=candidate_duration_ms,
    ), source


def test_widest_measured_duration_gap_stays_inside_the_tolerance() -> None:
    """ПРОЗАК — Сила: Spotify 309035 ms vs YouTube Music 310000 ms is the
    largest gap seen between two platforms holding the same recording."""
    widest_gap = max(
        abs(candidate_duration - reference_duration)
        for _, _, _, reference_duration, _, _, candidate_duration in REAL_CROSS_PLATFORM_PAIRS
    )
    assert widest_gap == 965
    assert widest_gap < DURATION_TOLERANCE_MS


@pytest.mark.parametrize(
    ("reference_artist", "candidate_artist"),
    [
        ("Burned Time Machine, 0%Mercury", "Burned Time Machine & 0%Mercury"),
        ("Burned Time Machine, 0%Mercury", "0%Mercury, Burned Time Machine"),
        ("hackedface", "Hackedface"),
        ("АННА", "Анна"),
        ("Тол", "ТОЛ"),
    ],
)
def test_normalize_artist_set_absorbs_real_crediting_differences(
    reference_artist: str, candidate_artist: str
) -> None:
    assert normalize_artist_set(reference_artist) == normalize_artist_set(candidate_artist)


@pytest.mark.parametrize(
    ("topic_channel", "plain_name"),
    [
        ("Анна - Topic", "АННА"),
        ("Burned Time Machine - Topic", "Burned Time Machine"),
        ("Hackedface - Topic", "hackedface"),
    ],
)
def test_normalize_artist_set_strips_the_real_topic_channel_suffix(
    topic_channel: str, plain_name: str
) -> None:
    """song.link returns the YouTube channel name as `artistName`, and for
    an auto-generated recording that name carries a `- Topic` tail."""
    assert normalize_artist_set(topic_channel) == normalize_artist_set(plain_name)


def test_is_match_rejects_same_word_different_song() -> None:
    # ytmusicapi live result for "Krovna — Berserker": a different artist's
    # track whose title merely contains the word "Berserker".
    assert not is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=263000,
        candidate_artist="Runes of the North",
        candidate_title="Bjarkamál (The Berserker's Call)",
        candidate_duration_ms=210000,
    )


@pytest.mark.parametrize(
    ("candidate_artist", "candidate_title", "candidate_duration_ms"),
    [
        ("Gunship, Tyler Bates", "Berserker (feat. Dave Lombardo)", 335000),
        ("Dal Av", "Berserker", 238000),
        ("RAVN", "Berserker", 380000),
        ("Dahako, HENSEI", "BERSERKER", 122000),
        ("Korbl", "Berserker", 116000),
    ],
)
def test_is_match_rejects_the_five_real_berserker_impostors(
    candidate_artist: str, candidate_title: str, candidate_duration_ms: int
) -> None:
    """all five came back in one live ytmusicapi search for "Krovna —
    Berserker" with a title that is exactly equal after normalization."""
    assert normalize_text(candidate_title) == "berserker"
    assert not is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=262105,
        candidate_artist=candidate_artist,
        candidate_title=candidate_title,
        candidate_duration_ms=candidate_duration_ms,
    )


def test_is_match_rejects_a_same_title_track_by_another_ukrainian_band() -> None:
    # live ytmusicapi result for "Тол — Сумніви": KOLORYT has a song of the
    # very same name, and only the artist set tells them apart.
    assert not is_match(
        reference_artist="Тол",
        reference_title="Сумніви",
        reference_duration_ms=293133,
        candidate_artist="KOLORYT",
        candidate_title="Сумніви",
        candidate_duration_ms=146000,
    )


@pytest.mark.parametrize(
    ("live_title", "expected"),
    [
        ("Артерія (Acoustic Version)", "артерія acoustic version"),
        ("snow (Extended Version)", "snow extended version"),
        ("oxygen (defacement version)", "oxygen defacement version"),
        ("Berserker - Cutdown", "berserker cutdown"),
        ("Церемонія (Прощання)", "церемонія прощання"),
    ],
)
def test_normalize_text_keeps_real_bracketed_tails(live_title: str, expected: str) -> None:
    """every title here came back live: the first four mark a different
    recording, the fifth is simply part of a reference track's own name."""
    assert normalize_text(live_title) == expected


@pytest.mark.parametrize("value", [None, 0, -1])
def test_known_duration_treats_a_non_positive_value_as_unknown(value: int | None) -> None:
    assert known_duration(value) is None


@pytest.mark.parametrize("value", [1, 965, 293133])
def test_known_duration_passes_a_real_length_through(value: int) -> None:
    assert known_duration(value) == value


@pytest.mark.parametrize(
    ("reference_artist", "reference_title", "candidate_duration_ms"),
    [
        ("ТОЛ", "Сумніви", 293133),
        ("hackedface", "but wait...", 264000),
        ("Krovna", "Berserker", 262105),
        ("ПРОЗАК", "Сила", 309035),
        ("Hackedface", "live like a saint/die like a sinner", 157058),
    ],
)
def test_is_match_treats_songlinks_zero_duration_as_unknown(
    reference_artist: str, reference_title: str, candidate_duration_ms: int
) -> None:
    """song.link's YouTube entity reports `duration: 0` for these five; read
    literally, 0 would reject every correct Spotify candidate."""
    assert is_match(
        reference_artist=reference_artist,
        reference_title=reference_title,
        reference_duration_ms=0,
        candidate_artist=reference_artist,
        candidate_title=reference_title,
        candidate_duration_ms=candidate_duration_ms,
    )


def test_is_match_rejects_duration_outside_tolerance() -> None:
    assert not is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=263000,
        candidate_artist="Krovna",
        candidate_title="Berserker",
        candidate_duration_ms=263000 + DURATION_TOLERANCE_MS + 1,
    )


def test_is_match_accepts_duration_at_tolerance_boundary() -> None:
    assert is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=263000,
        candidate_artist="Krovna",
        candidate_title="Berserker",
        candidate_duration_ms=263000 + DURATION_TOLERANCE_MS,
    )


def test_is_match_ignores_duration_when_either_side_is_unknown() -> None:
    assert is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=None,
        candidate_artist="Krovna",
        candidate_title="Berserker",
        candidate_duration_ms=999999,
    )


def test_is_match_rejects_title_mismatch() -> None:
    assert not is_match(
        reference_artist="Krovna",
        reference_title="Berserker",
        reference_duration_ms=None,
        candidate_artist="Krovna",
        candidate_title="Some Other Song",
        candidate_duration_ms=None,
    )


@pytest.mark.parametrize(
    "isrc",
    ["GBAYE9200070", "SE6A92327148", "UAA1Y2500001"],
)
def test_is_valid_isrc_accepts_well_formed_identifiers(isrc: str) -> None:
    assert is_valid_isrc(isrc) is True


@pytest.mark.parametrize(
    "isrc",
    [
        None,
        "",
        "GBAYE920007",
        "gbaye9200070",
        "GBAYE92000700",
        "GB/../track/1",
        "GBAYE9200070?x=1",
        "GBAYE920007A",
    ],
)
def test_is_valid_isrc_rejects_anything_that_could_reshape_a_url(isrc: str | None) -> None:
    """the value is pasted into `/track/isrc:<isrc>`, so a stray `/` or `?` from a
    source would change which endpoint we call."""
    assert is_valid_isrc(isrc) is False


def test_is_valid_spotify_id_accepts_a_real_base62_id() -> None:
    assert is_valid_spotify_id("6N9PS4QXF1D0OWPk0Sxtb4") is True


@pytest.mark.parametrize(
    "spotify_id",
    [
        None,
        "",
        "album123",
        "6N9PS4QXF1D0OWPk0Sxtb",
        "6N9PS4QXF1D0OWPk0Sxtb4x",
        "6N9PS4QXF1D0OWPk0Sxt 4",
        "../../v1/me",
    ],
)
def test_is_valid_spotify_id_rejects_anything_that_could_reshape_a_url(
    spotify_id: str | None,
) -> None:
    """the id goes into `/v1/tracks/<id>` and into an `href`; a space alone is a
    Telegram 400 on the whole post."""
    assert is_valid_spotify_id(spotify_id) is False


def test_is_valid_youtube_video_id_accepts_a_real_id() -> None:
    assert is_valid_youtube_video_id("n4bPzdIs_nc") is True


@pytest.mark.parametrize(
    "video_id",
    [
        None,
        "",
        "n4bPzdIs_n",
        "n4bPzdIs_ncx",
        "n4bPzdIs nc",
        "../../watch",
        "n4bPzdIs_n%",
    ],
)
def test_is_valid_youtube_video_id_rejects_anything_that_could_reshape_a_url(
    video_id: str | None,
) -> None:
    """the id is source-supplied and goes into a `watch?v=` URL and into `yt-dlp`,
    the same reason the ISRC is validated."""
    assert is_valid_youtube_video_id(video_id) is False


def make_candidate(
    title: str, artist: str = "Radiohead", url: str = "https://x/1"
) -> TrackCandidate:
    return TrackCandidate(
        platform="deezer",
        url=url,
        source="deezer",
        artist_name=artist,
        title=title,
        duration_ms=238_000,
    )


def test_the_first_candidate_that_matches_is_returned() -> None:
    wrong = make_candidate("Other Song", url="https://x/0")
    first = make_candidate("Creep", url="https://x/1")
    second = make_candidate("Creep", url="https://x/2")

    found = find_matching_candidate(
        [wrong, first, second], artist="Radiohead", title="Creep", duration_ms=238_000
    )

    assert found is first


def test_no_matching_candidate_gives_none() -> None:
    candidates = [make_candidate("Other Song"), make_candidate("Creep", artist="Someone Else")]

    assert (
        find_matching_candidate(candidates, artist="Radiohead", title="Creep", duration_ms=238_000)
        is None
    )
    assert find_matching_candidate([], artist="Radiohead", title="Creep", duration_ms=None) is None


def test_the_rejection_warning_names_the_source_the_count_and_the_reference() -> None:
    assert rejected_candidates_warning("itunes", 3, "Radiohead", "Creep") == (
        "itunes: 3 кандидат(и) відхилено звіркою для «Radiohead — Creep»"
    )
