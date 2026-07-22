from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.metrics import LEDGER_INVARIANT_VIOLATIONS_TOTAL

logger = structlog.get_logger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def envelope(
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


# Fallback codes for framework-raised HTTPExceptions
_STATUS_CODES = {
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "CONFLICT",
    429: "RATE_LIMITED",
}

# The deferred zero-sum/share-sum constraint triggers (see the initial-schema
# migration) raise a plain RAISE EXCEPTION with these phrases. post_ledger_event
# already pre-checks sums, so this should only ever fire if some future code
# bypasses that choke point — hence "must stay 0; alerts immediately".
_INVARIANT_MARKERS = ("entries sum to", "shares sum to")


def _is_invariant_violation(exc: DBAPIError) -> bool:
    return any(marker in str(exc.orig) for marker in _INVARIANT_MARKERS)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return envelope(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return envelope(422, "VALIDATION_ERROR", "Invalid request", {"errors": exc.errors()})

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        fallback = "VALIDATION_ERROR" if exc.status_code < 500 else "INTERNAL"
        code = _STATUS_CODES.get(exc.status_code, fallback)
        return envelope(exc.status_code, code, str(exc.detail))

    @app.exception_handler(DBAPIError)
    async def dbapi_error_handler(request: Request, exc: DBAPIError) -> JSONResponse:
        if _is_invariant_violation(exc):
            LEDGER_INVARIANT_VIOLATIONS_TOTAL.inc()
            logger.critical("ledger.invariant_violation", error=str(exc.orig))
        return envelope(500, "INTERNAL", "Internal error")
