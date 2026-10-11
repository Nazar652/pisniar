"""The rule for a text the admin saves, pure: spaces collapsed, the stored-text limits, times
inside the track and strictly rising. The first line that breaks it is named with its number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pisniar.db.models import MAX_LYRIC_LINES, MAX_LYRIC_TEXT_LENGTH, MAX_LYRICS_TOTAL_CHARACTERS

TOO_MANY_LINES_MESSAGE = "Рядків забагато: дозволено не більше {limit}, а рядок {number} уже зайвий."
LINE_TOO_LONG_MESSAGE = "Рядок {number} задовгий: {length} символів, дозволено не більше {limit}."
EMPTY_UNTIMED_MESSAGE = "Рядок {number} порожній і без часу — порожній рядок має бути паузою з часом."
TIME_OUT_OF_TRACK_MESSAGE = "Час рядка {number} ({start_ms} мс) поза треком: від 0 до {limit} мс."
TIME_NOT_RISING_MESSAGE = "Час рядка {number} ({start_ms} мс) не пізніший за попередній відстуканий ({previous_ms} мс)."
TEXT_TOO_LONG_MESSAGE = "Текст завеликий: разом з рядка {number} понад {limit} символів."


@dataclass(frozen=True)
class LineDraft:
    """a line as the editor sent it; `start_ms` is `None` for a line not yet timed."""

    text: str
    start_ms: int | None


class LyricsRuleError(Exception):
    """`number` counts from 1, as the editor shows it."""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(message)
        self.number = number
        self.message = message


def normalized_text(text: str) -> str:
    return " ".join(text.split())


def plan_lyrics(lines: Sequence[LineDraft], audio_duration_ms: int) -> tuple[LineDraft, ...]:
    """the lines as they will be stored. An empty list is a text removed on purpose. A pause is an
    empty line with a time; an empty line without one says nothing and is refused."""
    planned: list[LineDraft] = []
    previous_timed_ms: int | None = None
    total_characters = 0

    for index, draft in enumerate(lines):
        number = index + 1
        text = normalized_text(draft.text)

        if number > MAX_LYRIC_LINES:
            raise LyricsRuleError(number, TOO_MANY_LINES_MESSAGE.format(limit=MAX_LYRIC_LINES, number=number))
        if len(text) > MAX_LYRIC_TEXT_LENGTH:
            raise LyricsRuleError(
                number,
                LINE_TOO_LONG_MESSAGE.format(number=number, length=len(text), limit=MAX_LYRIC_TEXT_LENGTH),
            )
        if not text and draft.start_ms is None:
            raise LyricsRuleError(number, EMPTY_UNTIMED_MESSAGE.format(number=number))

        if draft.start_ms is not None:
            if not 0 <= draft.start_ms < audio_duration_ms:
                raise LyricsRuleError(
                    number,
                    TIME_OUT_OF_TRACK_MESSAGE.format(number=number, start_ms=draft.start_ms, limit=audio_duration_ms),
                )
            if previous_timed_ms is not None and draft.start_ms <= previous_timed_ms:
                raise LyricsRuleError(
                    number,
                    TIME_NOT_RISING_MESSAGE.format(
                        number=number, start_ms=draft.start_ms, previous_ms=previous_timed_ms
                    ),
                )
            previous_timed_ms = draft.start_ms

        total_characters += len(text)
        if total_characters > MAX_LYRICS_TOTAL_CHARACTERS:
            raise LyricsRuleError(
                number,
                TEXT_TOO_LONG_MESSAGE.format(number=number, limit=MAX_LYRICS_TOTAL_CHARACTERS),
            )

        planned.append(LineDraft(text=text, start_ms=draft.start_ms))

    return tuple(planned)
