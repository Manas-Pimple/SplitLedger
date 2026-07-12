"""Done-gate: Redis outage → events retained; Redis back → relay publishes all,
exactly-once-marked, in per-house seq order. Runs against the real dev Redis."""

import json

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uuid6 import uuid7

from app.config import get_settings
from app.ledger import post_ledger_event
from app.models import Event
from app.models.ledger import LedgerEventKind
from app.outbox import relay_once
from tests.factories import make_house, make_user


async def _write_events(session: AsyncSession, n: int) -> object:
    house = await make_house(session)
    alice = await make_user(session)
    bob = await make_user(session)
    for i in range(n):
        await post_ledger_event(
            session,
            house_id=house.id,
            kind=LedgerEventKind.expense,
            ref_id=uuid7(),
            entries={alice.id: 100 + i, bob.id: -(100 + i)},
            event_type="expense.created",
            payload={"i": i},
            created_by=alice.id,
        )
    await session.commit()
    return house.id


async def test_outage_then_recovery_publishes_exactly_once(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    house_id = await _write_events(session, 5)  # -> 10 outbox rows (event + snapshot each)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Redis "down": unreachable port. Publish fails, transaction rolls back.
    dead_redis = Redis.from_url("redis://localhost:1", socket_connect_timeout=0.2)
    with pytest.raises(RedisConnectionError):
        await relay_once(factory, dead_redis)
    await dead_redis.aclose()

    unpublished = (
        await session.execute(
            select(Event).where(Event.house_id == house_id, Event.published_at.is_(None))
        )
    ).scalars()
    assert len(list(unpublished)) == 10  # outage lost nothing

    # Redis back: subscribe, relay, assert delivery in seq order
    redis = Redis.from_url(get_settings().redis_url)
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"house:{house_id}")
    await pubsub.get_message(timeout=1)  # consume the subscribe confirmation

    published = 0
    while (n := await relay_once(factory, redis)) > 0:
        published += n
    assert published >= 10  # may include other tests' leftover events

    received = []
    for _ in range(10):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
        assert msg is not None, "expected 10 messages on the house channel"
        received.append(json.loads(msg["data"]))

    # envelope shape per WEBSOCKET_PROTOCOL §3
    first = received[0]
    assert set(first) == {"type", "house_id", "seq", "event_type", "ts", "payload"}
    assert first["type"] == "event"
    assert first["house_id"] == str(house_id)

    seqs = [m["seq"] for m in received]
    assert seqs == sorted(seqs)  # per-house seq order preserved
    types = [m["event_type"] for m in received]
    assert types[::2] == ["expense.created"] * 5
    assert types[1::2] == ["balance.updated"] * 5

    # exactly-once-marked: everything published, second pass is a no-op
    session.expire_all()
    still_pending = list(
        (
            await session.execute(
                select(Event).where(
                    Event.house_id == house_id, Event.published_at.is_(None)
                )
            )
        ).scalars()
    )
    assert still_pending == []
    assert await relay_once(factory, redis) == 0

    await pubsub.aclose()  # type: ignore[no-untyped-call]
    await redis.aclose()


async def test_metrics_move(engine: AsyncEngine, session: AsyncSession) -> None:
    from app.metrics import OUTBOX_PENDING, REDIS_PUBLISH_TOTAL, WORKER

    await _write_events(session, 1)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(get_settings().redis_url)

    before = REDIS_PUBLISH_TOTAL.labels(worker=WORKER)._value.get()
    while await relay_once(factory, redis) > 0:
        pass
    after = REDIS_PUBLISH_TOTAL.labels(worker=WORKER)._value.get()
    assert after >= before + 2  # event + balance snapshot
    assert OUTBOX_PENDING._value.get() == 0

    await redis.aclose()
