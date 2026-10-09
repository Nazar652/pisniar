"""The waveform peaks file: its format constants, the writer's document and the validating
reader. A leaf module (logging and the stdlib only), so the Mini App can read the file the
worker wrote without importing ffmpeg or the tasks.
"""

from __future__ import annotations

import json
from pathlib import Path

from pisniar.logging import get_logger

logger = get_logger(__name__)

FORMAT_VERSION = 1
PEAKS_PER_SECOND = 20
PEAK_SCALE = 255

# one hour of audio at 20 values a second; the reader refuses anything longer.
MAX_PEAK_VALUES = 72_000
MAX_PEAKS_FILE_BYTES = 1_000_000


def peaks_document(values: list[int]) -> str:
    return json.dumps(
        {"format_version": FORMAT_VERSION, "peaks_per_second": PEAKS_PER_SECOND, "values": values},
        separators=(",", ":"),
    )


def read_peaks(path: Path) -> list[int] | None:
    """`None` with a warning for a missing, oversized or malformed file; pure, no ffmpeg."""
    try:
        if path.stat().st_size > MAX_PEAKS_FILE_BYTES:
            raise ValueError("файл peaks завеликий")
        document = json.loads(path.read_text(encoding="utf-8"))
        return validated_values(document)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("audio_peaks_unreadable", path=str(path), exc_info=True)
        return None


def validated_values(document: object) -> list[int]:
    if not isinstance(document, dict):
        raise ValueError("peaks не є об'єктом")
    if document.get("format_version") != FORMAT_VERSION:
        raise ValueError("інша версія формату peaks")
    if document.get("peaks_per_second") != PEAKS_PER_SECOND:
        raise ValueError("інша частота peaks")

    values = document.get("values")
    if not isinstance(values, list) or not 0 < len(values) <= MAX_PEAK_VALUES:
        raise ValueError("кількість значень peaks поза межами")

    checked: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= PEAK_SCALE:
            raise ValueError("значення peaks поза діапазоном 0-255")
        checked.append(value)
    return checked
