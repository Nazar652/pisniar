"""Which `initData` field names reach the log: the documented ones, by name only."""

from __future__ import annotations

import structlog.testing
from fastapi.testclient import TestClient
from support import ADMIN_ID, BOT_TOKEN, make_init_data

from pisniar.webapp.auth import INIT_DATA_FIELD_NAMES, init_data_field_names


def test_the_documented_fields_are_listed_sorted() -> None:
    init_data = make_init_data()

    names, unknown = init_data_field_names(init_data)

    assert names == ["auth_date", "hash", "query_id", "signature", "user"]
    assert unknown == 0


def test_a_field_outside_the_documented_list_is_counted_and_never_named() -> None:
    init_data = make_init_data(extra={"injected\nname": "x", "other": "y"})

    names, unknown = init_data_field_names(init_data)

    assert "other" not in names
    assert all("\n" not in name for name in names)
    assert unknown == 2


def test_a_malformed_string_gives_nothing() -> None:
    assert init_data_field_names("not a query string") == ([], 0)
    assert init_data_field_names("") == ([], 0)


def test_the_list_has_the_documented_signature_field() -> None:
    assert {"hash", "signature", "auth_date", "user"} <= INIT_DATA_FIELD_NAMES


def test_the_exchange_logs_field_names_and_the_signature_flag_but_no_value(
    client: TestClient,
) -> None:
    init_data = make_init_data()

    with structlog.testing.capture_logs() as captured:
        response = client.post("/api/session", json={"init_data": init_data})

    assert response.status_code == 200
    received = next(entry for entry in captured if entry["event"] == "webapp_init_data_received")
    assert received["fields"] == ["auth_date", "hash", "query_id", "signature", "user"]
    assert received["has_signature"] is True
    assert received["unknown_fields"] == 0
    rendered = repr(captured)
    assert BOT_TOKEN not in rendered
    assert "AAH-test" not in rendered
    assert str(ADMIN_ID) in rendered


def test_a_rejected_exchange_is_logged_with_its_fields_too(client: TestClient) -> None:
    with structlog.testing.capture_logs() as captured:
        response = client.post("/api/session", json={"init_data": "a=b"})

    assert response.status_code == 401
    events = [entry["event"] for entry in captured]
    assert events == ["webapp_init_data_received", "webapp_auth_rejected"]
