"""Domain errors at the HTTP boundary.

A domain error that reaches FastAPI as a plain exception makes the endpoint's span an ERROR,
so every "project not found" or "invalid name" would look like a server fault in a trace
backend. `PlatformRoute` therefore converts domain errors to an `HTTPException` *inside* the
endpoint call, which FastAPI's native telemetry treats as a normal client-error outcome.
The response body is unchanged.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from controlplane.domain.errors import (
    AlreadyExists,
    Conflict,
    DomainError,
    IllegalTransition,
    InvalidArgument,
    NotFound,
    PermissionDenied,
    Unauthenticated,
)

_STATUS_FOR: dict[type[DomainError], tuple[int, str]] = {
    NotFound: (status.HTTP_404_NOT_FOUND, "not_found"),
    AlreadyExists: (status.HTTP_409_CONFLICT, "already_exists"),
    Conflict: (status.HTTP_409_CONFLICT, "conflict"),
    IllegalTransition: (status.HTTP_409_CONFLICT, "illegal_transition"),
    InvalidArgument: (status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_argument"),
    Unauthenticated: (status.HTTP_401_UNAUTHORIZED, "unauthenticated"),
    PermissionDenied: (status.HTTP_403_FORBIDDEN, "permission_denied"),
}


def describe(exc: BaseException) -> tuple[int, str]:
    return next(
        (v for k, v in _STATUS_FOR.items() if isinstance(exc, k)),
        (status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error"),
    )


def error_response(exc: BaseException) -> JSONResponse:
    code, label = describe(exc)
    return JSONResponse(status_code=code, content={"error": {"code": label, "message": str(exc)}})


class DomainHttpError(HTTPException):
    """A domain error, as an HTTPException."""

    def __init__(self, domain: DomainError) -> None:
        super().__init__(status_code=describe(domain)[0], detail=str(domain))
        self.domain = domain


def handle_domain_error(_: Request, exc: Exception) -> JSONResponse:
    return error_response(exc.domain if isinstance(exc, DomainHttpError) else exc)


def _convert(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(endpoint):

        @functools.wraps(endpoint)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await endpoint(*args, **kwargs)
            except DomainError as exc:
                raise DomainHttpError(exc) from exc

        return async_wrapper

    @functools.wraps(endpoint)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return endpoint(*args, **kwargs)
        except DomainError as exc:
            raise DomainHttpError(exc) from exc

    return wrapper


class PlatformRoute(APIRoute):
    """Use as `APIRouter(route_class=PlatformRoute)`."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        super().__init__(path, _convert(endpoint), **kwargs)
