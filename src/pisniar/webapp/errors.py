"""One error shape for the whole API: `{"code", "message"}`, with no exception detail."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from pisniar.webapp.schemas import ApiError


class ApiProblem(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    body = ApiError(code=code, message=message).model_dump()
    return JSONResponse(body, status_code=status_code)


def register_error_handlers(app: FastAPI) -> None:
    async def on_problem(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, ApiProblem)
        return error_response(error.status_code, error.code, error.message)

    async def on_invalid_request(request: Request, error: Exception) -> JSONResponse:
        # the default 422 body echoes the offending input back, `initData` included.
        return error_response(422, "invalid_request", "Некоректний запит.")

    async def on_http_error(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, StarletteHTTPException)
        if error.status_code == 404:
            return error_response(404, "not_found", "Не знайдено.")
        if error.status_code == 405:
            return error_response(405, "method_not_allowed", "Метод не підтримується.")
        return error_response(error.status_code, "http_error", "Помилка запиту.")

    app.add_exception_handler(ApiProblem, on_problem)
    app.add_exception_handler(RequestValidationError, on_invalid_request)
    app.add_exception_handler(StarletteHTTPException, on_http_error)
