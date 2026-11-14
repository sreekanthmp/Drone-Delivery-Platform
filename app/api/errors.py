"""Platform-neutral error responses for the internal API.

Partner-facing routes never use these: they render errors through the partner's adapter.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.dispatch.exceptions import (
    CommandNotConfirmedError,
    DroneBusyError,
    DroneNotFoundError,
    DuplicateDroneError,
    FlightInProgressError,
    FlightNotFoundError,
    OrderNotDispatchableError,
    StartCommandFailedError,
)
from app.domain.exceptions import (
    DomainError,
    FlightPermissionError,
    InvalidDroneTransitionError,
    InvalidFlightTransitionError,
    InvalidTransitionError,
    OrderValidationError,
)
from app.services.exceptions import (
    ConcurrentUpdateError,
    DeliveryNotFoundError,
    DeliveryNotRetryableError,
    OrderNotFoundError,
)

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


async def _permission_refused(request: Request, exc: Exception) -> JSONResponse:
    """Rule 2: the compliance gate said no. Nothing was changed and nothing was sent."""
    return error_response(409, "permission_refused", str(exc))


async def _command_not_confirmed(request: Request, exc: Exception) -> JSONResponse:
    """The drone did not confirm the command. Nothing was recorded; retrying is safe."""
    return error_response(503, "command_not_confirmed", str(exc))


async def _start_failed(request: Request, exc: Exception) -> JSONResponse:
    """The drone did not take the start command; the flight was cleaned up (see the message)."""
    return error_response(502, "start_failed", str(exc))


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_error", path=request.url.path, exc_info=exc)
    return error_response(500, "internal_error", "internal error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(OrderNotFoundError, _not_found)
    app.add_exception_handler(DeliveryNotFoundError, _not_found)
    app.add_exception_handler(DeliveryNotRetryableError, _conflict)
    for not_found in (DroneNotFoundError, FlightNotFoundError):
        app.add_exception_handler(not_found, _not_found)
    for conflict in (
        InvalidFlightTransitionError,
        InvalidDroneTransitionError,
        OrderNotDispatchableError,
        FlightInProgressError,
        DroneBusyError,
        DuplicateDroneError,
    ):
        app.add_exception_handler(conflict, _conflict)
    app.add_exception_handler(FlightPermissionError, _permission_refused)
    app.add_exception_handler(CommandNotConfirmedError, _command_not_confirmed)
    app.add_exception_handler(StartCommandFailedError, _start_failed)
    app.add_exception_handler(OrderValidationError, _unprocessable)
    app.add_exception_handler(InvalidTransitionError, _conflict)
    app.add_exception_handler(ConcurrentUpdateError, _conflict)
    app.add_exception_handler(DomainError, _unprocessable)
    app.add_exception_handler(Exception, _unexpected)
