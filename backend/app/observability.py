"""Request-id + HTTP metrics middleware, structlog JSON config, and the
internal-port metrics server (OBSERVABILITY.md §1, §5). /metrics is never
proxied through Caddy — it's served on its own port (settings.metrics_port),
independent of the public API port.
"""

import logging
import socket
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS_TOTAL, metrics_app

logger = structlog.get_logger(__name__)


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            route = _route_template(request)
            HTTP_REQUEST_DURATION.labels(route=route).observe(time.perf_counter() - start)
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method, route=route, status=response.status_code
            ).inc()
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            structlog.contextvars.clear_contextvars()


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return str(route.path) if route is not None else "unmatched"


def _try_bind(port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
        sock.listen(100)
        return sock
    except OSError:
        sock.close()
        return None


async def serve_metrics_server(port: int) -> None:
    """Best-effort: with multiple uvicorn workers, only the first to bind
    wins — the others log and return rather than crash the worker process."""
    sock = _try_bind(port)
    if sock is None:
        logger.warning("metrics_port_unavailable", port=port)
        return
    config = uvicorn.Config(metrics_app, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve(sockets=[sock])
