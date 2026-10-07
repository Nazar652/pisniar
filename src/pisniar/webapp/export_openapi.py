"""Offline export of the API schema: no database, no `.env`, no network."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pisniar.config import Settings
from pisniar.webapp.app import create_webapp

DEFAULT_OUTPUT = Path("frontend/openapi.json")


def build_openapi_schema() -> dict[str, Any]:
    settings = Settings(
        _env_file=None,
        bot_token="0:schema-export",
        admin_ids="1",
        alert_chat_id=1,
        channel_id=-1,
        database_url="postgresql+asyncpg://schema:export@localhost/schema",
        spotify_client_id="schema-export",
        spotify_client_secret="schema-export",
    )
    return create_webapp(settings).openapi()


def render_schema(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="export_openapi")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    # `newline="\n"`: a Windows host must not turn the file into CRLF.
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_schema(build_openapi_schema()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
