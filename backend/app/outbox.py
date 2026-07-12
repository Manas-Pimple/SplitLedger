"""Transactional outbox relay per ARCHITECTURE.md §3.

Each API worker runs relay_loop as a background task. relay_once claims
unpublished events with FOR UPDATE SKIP LOCKED (workers never fight over the
same rows), publishes them to Redis channel house:{house_id}, and marks them
published in the same transaction. Redis failure rolls the batch back — events
stay pending and are retried; delivery is at-least-once, clients dedupe by seq.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.metrics import (
    OUTBOX_PENDING,
    OUTBOX_PUBLISH_LATENCY,
    REDIS_PUBLISH_TOTAL,
    WORKER,
)
from app.models import Event

logger = logging.getLogger(__name__)

BATCH = 100
BUSY_INTERVAL = 0.1
IDLE_INTERVAL = 0.5
ERROR_BACKOFF = 2.0


def envelope(event: Event) -> str:
    """WEBSOCKET_PROTOCOL.md §3 wire shape — identical to the outbox row."""
    return json.dumps(
        {
            "type": "event",
            "house_id": str(event.house_id),
            "seq": event.seq,
            "event_type": event.type,
            "ts": event.created_at.isoformat(),
            "payload": event.payload,
        }
    )


async def relay_once(
    factory: async_sessionmaker[AsyncSession], redis: Redis
) -> int:
    """Publish one batch. Returns number published (0 = outbox drained)."""
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Event)
                    .where(Event.published_at.is_(None))
                    .order_by(Event.house_id, Event.seq)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        now = datetime.now(UTC)
        for event in rows:
            await redis.publish(f"house:{event.house_id}", envelope(event))
            event.published_at = now
            OUTBOX_PUBLISH_LATENCY.observe((now - event.created_at).total_seconds())
            REDIS_PUBLISH_TOTAL.labels(worker=WORKER).inc()
        await session.commit()

        pending = (
            await session.execute(
                select(func.count()).select_from(Event).where(Event.published_at.is_(None))
            )
        ).scalar_one()
        OUTBOX_PENDING.set(pending)
        return len(rows)


async def relay_loop(
    factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    stop: asyncio.Event | None = None,
) -> None:
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            published = await relay_once(factory, redis)
            delay = BUSY_INTERVAL if published else IDLE_INTERVAL
        except Exception:
            # Redis or DB hiccup: batch rolled back, events remain pending
            logger.exception("outbox relay batch failed; retrying")
            delay = ERROR_BACKOFF
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass
