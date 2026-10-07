"""The JSON readers every adapter and the stored-row readers parse through: a wrong
shape is "absent", never an exception, and a `bool` is never a number.
"""

from __future__ import annotations

import pytest

from pisniar.resolve.json_utils import as_dict, as_int, as_list, as_nonempty_str, as_str


def test_as_dict_passes_a_dict_and_replaces_anything_else_with_an_empty_one() -> None:
    value = {"a": 1}

    assert as_dict(value) is value
    assert as_dict([1]) == {}
    assert as_dict(None) == {}
    assert as_dict("text") == {}


def test_as_list_passes_a_list_and_replaces_anything_else_with_an_empty_one() -> None:
    value = [1, 2]

    assert as_list(value) is value
    assert as_list({"a": 1}) == []
    assert as_list(None) == []
    assert as_list("text") == []


def test_as_str_keeps_any_string_including_the_empty_one() -> None:
    assert as_str("text") == "text"
    assert as_str("") == ""
    assert as_str(5) is None
    assert as_str(None) is None


@pytest.mark.parametrize("value", ["text", "  "])
def test_as_nonempty_str_keeps_a_string_with_characters(value: str) -> None:
    assert as_nonempty_str(value) == value


@pytest.mark.parametrize("value", ["", None, 5, ["text"], {"a": "b"}])
def test_as_nonempty_str_drops_everything_else(value: object) -> None:
    assert as_nonempty_str(value) is None


@pytest.mark.parametrize("value", [0, 7, -3, 2_000_000_000_000])
def test_as_int_keeps_an_integer(value: int) -> None:
    assert as_int(value) == value


@pytest.mark.parametrize("value", [True, False, 1.5, "7", None, [7]])
def test_as_int_drops_a_bool_a_float_a_string_and_the_rest(value: object) -> None:
    assert as_int(value) is None
