"""`build_bot()`: the shared client stays `parse_mode=None` globally. The same
client sends alerts, whose text routinely contains `<` from tracebacks and URLs;
a global `parse_mode="HTML"` would make Telegram reject or interpret them.
"""

from __future__ import annotations

from pisniar.config import get_settings
from pisniar.telegram.client import build_bot


async def test_shared_bot_has_no_global_parse_mode() -> None:
    bot = build_bot(get_settings())
    try:
        assert bot.default.parse_mode is None
    finally:
        await bot.session.close()
