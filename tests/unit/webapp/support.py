"""Helpers shared by the Mini App tests: a signer that mirrors Telegram's `initData`
algorithm independently of aiogram.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

BOT_TOKEN = "123456:webapp-test-token"
ADMIN_ID = 111
NOW = 1_800_000_000


def sign_fields(fields: dict[str, str], bot_token: str) -> str:
    """The documented algorithm: HMAC-SHA256 over the sorted `key=value` lines."""
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def make_init_data(
    *,
    user_id: object = ADMIN_ID,
    auth_date: int = NOW,
    bot_token: str = BOT_TOKEN,
    extra: dict[str, str] | None = None,
) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAH-test",
        "signature": "c2lnbmF0dXJlLW5vdC1jaGVja2VkLWhlcmU",
        "user": json.dumps({"id": user_id, "first_name": "Test"}),
        **(extra or {}),
    }
    return sign_fields(fields, bot_token)
