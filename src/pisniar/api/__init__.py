"""HTTP health/readiness API, served inside the `bot` process.

Deliberately does not import anything from `pisniar.bot` (boundary check:
`api` has no reverse dependency on the bot's handlers/middlewares).
"""
