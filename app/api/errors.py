"""Platform-neutral error responses for the internal API.

Partner-facing routes never use these: they render errors through the partner's adapter.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    DomainError,
    InvalidTransitionError,
    OrderValidationError,
)
from app.services.exceptions import ConcurrentUpdateError, OrderNotFoundError

logger = structlog.get_logger(__name__)


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


async def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return error_response(404, "not_found", str(exc))


async def _unprocessable(request: Request, exc: Exception) -> JSONResponse:
    return error_response(422, "invalid_request", str(exc))


async def _conflict(request: Request, exc: Exception) -> JSONResponse:
    return error_response(409, "conflict", str(exc))


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_error", path=request.url.path, exc_info=exc)
    return error_response(500, "internal_error", "internal error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(OrderNotFoundError, _not_found)
    app.add_exception_handler(OrderValidationError, _unprocessable)
    app.add_exception_handler(InvalidTransitionError, _conflict)
    app.add_exception_handler(ConcurrentUpdateError, _conflict)
    app.add_exception_handler(DomainError, _unprocessable)
    app.add_exception_handler(Exception, _unexpected)
