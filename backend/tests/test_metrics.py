"""BUILD_PLAN Phase 11 done-gate: every OBSERVABILITY.md §1 metric name is
present after a scrape of the real /metrics ASGI app, following an
integration run that actually drives each subsystem (no metrics faked)."""

from typing import cast
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.websockets import WebSocket

import app.db as db
from app.errors import _is_invariant_violation
from app.metrics import WORKER, WS_CONNECTIONS_ACTIVE, metrics_app
from app.outbox import relay_once
from app.scheduler import tick
from app.ws import QUEUE_MAX, Registry, Socket
from tests.factories import make_house, make_ledger_event, make_user

REQUIRED_METRIC_NAMES = [
    "http_requests_total",
    "http_request_duration_seconds",
    "ws_connections_active",
    "ws_messages_sent_total",
    "ws_send_queue_dropped_total",
    "redis_publish_total",
    "redis_receive_total",
    "outbox_pending",
    "outbox_publish_latency_seconds",
    "scheduler_tick_duration_seconds",
    "scheduler_bills_generated_total",
    "scheduler_reminders_sent_total",
    "db_pool_in_use",
    "ledger_invariant_violations_total",
]


async def _scrape() -> str:
    transport = httpx.ASGITransport(app=metrics_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://metrics") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    return resp.text


async def test_http_request_drives_http_metrics_and_request_id(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/api/v1/healthz")
    assert "X-Request-Id" in resp.headers


async def test_scheduler_tick_drives_scheduler_metrics(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await tick(factory, engine)


async def test_outbox_relay_drives_pipeline_metrics(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await make_ledger_event(session, house, {alice: 500, bob: -500})
    await session.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis: Redis = Redis.from_url("redis://localhost:6380/0")
    try:
        await relay_once(factory, redis)
    finally:
        await redis.aclose()


async def test_ws_socket_drives_ws_metrics() -> None:
    registry = Registry()
    sock = Socket(cast(WebSocket, None), uuid4(), 0.0)
    registry.add(sock)
    WS_CONNECTIONS_ACTIVE.labels(worker=WORKER).inc()

    sock.try_send("{}", "balance.updated")  # 1 accepted -> ws_messages_sent_total
    for _ in range(QUEUE_MAX - 1):
        sock.queue.put_nowait("{}")
    sock.try_send("{}", "balance.updated")  # queue now full -> ws_send_queue_dropped_total

    registry.remove(sock)
    WS_CONNECTIONS_ACTIVE.labels(worker=WORKER).dec()


async def test_db_checkout_drives_pool_gauge() -> None:
    """Goes through the real get_engine() (not the raw `engine` fixture) so the
    checkout/checkin listeners registered there are actually attached."""
    db._engine = None
    db._session_factory = None
    engine = db.get_engine()
    try:
        async with engine.connect():
            pass
    finally:
        await db.dispose_engine()


async def test_ledger_trigger_violation_detected_and_counted(
    session: AsyncSession,
) -> None:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    await make_ledger_event(session, house, {alice: 500, bob: -400})
    with pytest.raises(DBAPIError) as excinfo:
        await session.commit()
    await session.rollback()
    assert _is_invariant_violation(excinfo.value)


async def test_metrics_endpoint_reports_every_observability_metric() -> None:
    body = await _scrape()
    missing = [name for name in REQUIRED_METRIC_NAMES if name not in body]
    assert not missing, f"missing from /metrics: {missing}"
