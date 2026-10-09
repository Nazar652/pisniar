"""The peaks file format: the writer's document and the validating reader that the worker and
the Mini App share, and the imports that keep the module a leaf.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pisniar.worker import peaks_format
from pisniar.worker.peaks_format import (
    MAX_PEAK_VALUES,
    MAX_PEAKS_FILE_BYTES,
    peaks_document,
    read_peaks,
    validated_values,
)


@pytest.fixture(autouse=True)
def format_logger(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """a stand-in: a cached structlog proxy defeats `capture_logs()`."""
    fake_logger = MagicMock()
    monkeypatch.setattr(peaks_format, "logger", fake_logger)
    return fake_logger


def write_document(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_the_document_names_its_format_and_rate() -> None:
    assert json.loads(peaks_document([0, 255])) == {
        "format_version": 1,
        "peaks_per_second": 20,
        "values": [0, 255],
    }


def test_a_valid_file_reads_back_its_values(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(peaks_document([0, 7, 255]), encoding="utf-8")

    assert read_peaks(path) == [0, 7, 255]


def test_a_missing_file_is_none_without_a_warning(tmp_path: Path, format_logger: MagicMock) -> None:
    assert read_peaks(tmp_path / "absent.json") is None
    format_logger.warning.assert_not_called()


@pytest.mark.parametrize(
    "document",
    [
        [1, 2, 3],
        {"format_version": 2, "peaks_per_second": 20, "values": [1]},
        {"format_version": 1, "peaks_per_second": 10, "values": [1]},
        {"format_version": 1, "peaks_per_second": 20, "values": []},
        {"format_version": 1, "peaks_per_second": 20, "values": "abc"},
        {"format_version": 1, "peaks_per_second": 20, "values": [256]},
        {"format_version": 1, "peaks_per_second": 20, "values": [-1]},
        {"format_version": 1, "peaks_per_second": 20, "values": [1.5]},
        {"format_version": 1, "peaks_per_second": 20, "values": [True]},
        {"format_version": 1, "peaks_per_second": 20, "values": [0] * (MAX_PEAK_VALUES + 1)},
    ],
)
def test_a_malformed_file_is_none_with_a_warning(
    tmp_path: Path, format_logger: MagicMock, document: object
) -> None:
    path = tmp_path / "p.json"
    write_document(path, document)

    assert read_peaks(path) is None
    assert format_logger.warning.call_args.args[0] == "audio_peaks_unreadable"


def test_the_longest_accepted_file_is_one_hour(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(peaks_document([255] * MAX_PEAK_VALUES), encoding="utf-8")

    values = read_peaks(path)

    assert values is not None
    assert len(values) == 72_000


@pytest.mark.parametrize("content", [b"not json", b"\xff\xfe\x00", b"{"])
def test_a_file_that_is_not_json_is_none_with_a_warning(
    tmp_path: Path, format_logger: MagicMock, content: bytes
) -> None:
    path = tmp_path / "p.json"
    path.write_bytes(content)

    assert read_peaks(path) is None
    format_logger.warning.assert_called_once()


def test_an_oversized_file_is_refused_before_it_is_parsed(
    tmp_path: Path, format_logger: MagicMock
) -> None:
    path = tmp_path / "p.json"
    path.write_bytes(b" " * (MAX_PEAKS_FILE_BYTES + 1))

    assert read_peaks(path) is None
    format_logger.warning.assert_called_once()


def test_validated_values_returns_the_list_of_a_good_document() -> None:
    document = {"format_version": 1, "peaks_per_second": 20, "values": [1, 2]}

    assert validated_values(document) == [1, 2]


def test_the_module_imports_nothing_but_logging_and_the_stdlib() -> None:
    source = Path(peaks_format.__file__).read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert {name for name in imported if name.startswith("pisniar")} == {"pisniar.logging"}
