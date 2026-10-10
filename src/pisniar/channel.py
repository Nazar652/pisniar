"""The channel's identity in one place: `CHANNEL_LINK_URL` is what every caption links to, while `Settings.channel_id`
is where the post is actually sent. Neither can be derived from the other, so the two are reconciled at runtime instead
— here at startup, and against `get_chat()` in `telegram.publish`.
"""

from __future__ import annotations

from pisniar.logging import get_logger

logger = get_logger(__name__)

CHANNEL_LINK_TEXT = "Піснярня 🇺🇦"
CHANNEL_LINK_URL = "https://t.me/pisniarnia"
CHANNEL_USERNAME = CHANNEL_LINK_URL.rsplit("/", 1)[-1]


def warn_if_alerts_share_the_channel(channel_id: int, alert_chat_id: int) -> bool:
    """a warning, not a refusal: a test channel *is* the alert chat on purpose, while on the real one this leaks error
    texts into a public feed."""
    if channel_id != alert_chat_id:
        return False

    logger.warning("alert_chat_is_the_publish_channel", channel_id=channel_id)
    return True
