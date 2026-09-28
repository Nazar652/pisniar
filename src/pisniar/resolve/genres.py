"""Deezer genre-id -> Ukrainian name dictionary. Keyed by `id`, not the
localized `name`: Deezer localizes names by request IP (a Romanian IP gets
"Alternativă" for id 85).
"""

from __future__ import annotations

from pisniar.logging import get_logger

logger = get_logger(__name__)

# id 0 ("All") is Deezer's "unclassified" sentinel, not a real genre.
DEEZER_UNCLASSIFIED_GENRE_ID = 0

# id -> (verified English name, Ukrainian name for the post).
DEEZER_GENRE_NAMES: dict[int, tuple[str, str]] = {
    2: ("African Music", "африканська музика"),
    16: ("Asian Music", "азійська музика"),
    75: ("Brazilian Music", "бразильська музика"),
    81: ("Indian Music", "індійська музика"),
    85: ("Alternative", "альтернатива"),
    95: ("Kids", "дитяча музика"),
    98: ("Classical", "класична музика"),
    106: ("Electro", "електроніка"),
    113: ("Dance", "денс"),
    116: ("Rap/Hip Hop", "хіп-хоп"),
    129: ("Jazz", "джаз"),
    132: ("Pop", "поп"),
    144: ("Reggae", "реггі"),
    152: ("Rock", "рок"),
    153: ("Blues", "блюз"),
    165: ("R&B", "рнб"),
    169: ("Soul & Funk", "соул і фанк"),
    173: ("Films/Games", "саундтрек"),
    197: ("Latin Music", "латиноамериканська музика"),
    464: ("Metal", "метал"),
    466: ("Folk", "фолк"),
}


def deezer_genre_name(genre_id: int, fallback_english_name: str) -> str | None:
    """Never falls back to `fallback_english_name` (may be IP-localized); that
    value is for the WARN log only."""
    if genre_id == DEEZER_UNCLASSIFIED_GENRE_ID:
        return None

    entry = DEEZER_GENRE_NAMES.get(genre_id)
    if entry is None:
        logger.warning(
            "deezer_genre_id_unknown", genre_id=genre_id, english_name=fallback_english_name
        )
        return None
    return entry[1]
