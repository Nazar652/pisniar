"""The uvicorn access log without query strings: the signed audio URL carries its token there."""

from __future__ import annotations

import logging

ACCESS_LOGGER_NAME = "uvicorn.access"
# uvicorn logs (client address, method, path with query, HTTP version, status code)
REQUEST_TARGET_INDEX = 2
ACCESS_ARGUMENT_COUNT = 5


class QueryStrippingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        if isinstance(arguments, tuple) and len(arguments) == ACCESS_ARGUMENT_COUNT:
            request_target = arguments[REQUEST_TARGET_INDEX]
            if isinstance(request_target, str):
                path = request_target.partition("?")[0]
                record.args = (*arguments[:REQUEST_TARGET_INDEX], path, *arguments[3:])
        return True


def install_access_log_filter() -> None:
    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    if not any(isinstance(item, QueryStrippingFilter) for item in access_logger.filters):
        access_logger.addFilter(QueryStrippingFilter())
