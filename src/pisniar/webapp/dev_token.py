"""CLI only: print a real session token for an admin, for a browser outside Telegram.

There is no HTTP route for this and none can be switched on; the token is signed with the
same key as a production one and only ever goes to the caller's terminal.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

from pisniar.config import get_settings
from pisniar.webapp.auth import issue_session_token


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev_token", description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    arguments = parser.parse_args(argv)

    settings = get_settings()
    if arguments.user_id not in settings.admin_ids:
        print("user id is not in ADMIN_IDS", file=sys.stderr)
        return 1

    token = issue_session_token(
        arguments.user_id,
        settings.bot_token.get_secret_value(),
        int(time.time()),
        settings.webapp_session_ttl_seconds,
    )
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
