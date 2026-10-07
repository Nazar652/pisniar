"""Narrow `object`-typed JSON values from untrusted API responses without
`ty` losing track of the type — every source adapter parses through these.
"""

from __future__ import annotations


def as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def as_nonempty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def as_int(value: object) -> int | None:
    """a `bool` is an `int` to Python but never a number in these payloads."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None
