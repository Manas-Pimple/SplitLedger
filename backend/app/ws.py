"""WebSocket gateway per WEBSOCKET_PROTOCOL.md.

One connection per client session, auth-first-frame, per-house subscribe with
seq replay, application-level heartbeat, mid-connection reauth, bounded send
queues (slow consumer → close 1012, correctness preserved by seq/replay), and
membership-revocation eviction (member.left → close 4403).

Per-worker state: a Registry of live sockets plus one Redis PSUBSCRIBE
consumer fanning backplane messages to local sockets — this is what makes
multi-worker fan-out correct (event produced via worker B reaches a socket
held by worker A).
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.metrics import (
    REDIS_RECEIVE_TOTAL,
    WORKER,
    WS_CONNECTIONS_ACTIVE,
    WS_MESSAGES_SENT_TOTAL,
    WS_SEND_QUEUE_DROPPED_TOTAL,
)
from app.models import Dispute, Event, HouseMembership, User
from app.models.house import MembershipStatus
from app.permissions import resolve_role
from app.security import decode_token_payload

logger = logging.getLogger(__name__)
router = APIRouter()

AUTH_TIMEOUT = 5.0
PING_INTERVAL = 25.0
MAX_MISSED_PONGS = 2
REAUTH_GRACE = 30.0
QUEUE_MAX = 200
REPLAY_LIMIT = 10_000


class Socket:
    def __init__(self, ws: WebSocket, user_id: UUID, token_exp: float) -> None:
        self.ws = ws
        self.user_id = user_id
        self.token_exp = token_exp
        self.houses: set[UUID] = set()
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.missed_pongs = 0
        self.reauth_deadline: float | None = None
        self.close_code: int | None = None  # set → tasks unwind

    def try_send(self, raw: str, event_type: str) -> None:
        try:
            self.queue.put_nowait(raw)
            WS_MESSAGES_SENT_TOTAL.labels(event_type=event_type).inc()
        except asyncio.QueueFull:
            WS_SEND_QUEUE_DROPPED_TOTAL.inc()
            self.close_code = 1012  # client replays via seq on reconnect

    def send_json(self, obj: dict[str, Any], event_type: str) -> None:
        self.try_send(json.dumps(obj), event_type)


class Registry:
    def __init__(self) -> None:
        self.by_house: dict[UUID, set[Socket]] = defaultdict(set)
        self.by_user: dict[UUID, set[Socket]] = defaultdict(set)

    def add(self, sock: Socket) -> None:
        self.by_user[sock.user_id].add(sock)

    def subscribe(self, sock: Socket, house_id: UUID) -> None:
        sock.houses.add(house_id)
        self.by_house[house_id].add(sock)

    def unsubscribe(self, sock: Socket, house_id: UUID) -> None:
        sock.houses.discard(house_id)
        self.by_house[house_id].discard(sock)

    def remove(self, sock: Socket) -> None:
        for house_id in list(sock.houses):
            self.by_house[house_id].discard(sock)
        self.by_user[sock.user_id].discard(sock)


registry = Registry()  # per-process, one per worker


async def backplane_consumer(redis: Redis, stop: asyncio.Event) -> None:
    """PSUBSCRIBE house:* and fan messages out to local sockets. Survives Redis
    restarts by reconnecting with backoff."""
    while not stop.is_set():
        try:
            pubsub = redis.pubsub()
            await pubsub.psubscribe("house:*")
            while not stop.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
                if msg is None:
                    continue
                REDIS_RECEIVE_TOTAL.labels(worker=WORKER).inc()
                _dispatch(msg["data"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("backplane consumer error; reconnecting")
            await asyncio.sleep(1)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]


def _dispatch(raw: bytes | str) -> None:
    data = json.loads(raw)
    house_id = UUID(data["house_id"])
    event_type: str = data["event_type"]
    text = raw.decode() if isinstance(raw, bytes) else raw

    if event_type == "member.left":
        evicted = UUID(data["payload"]["user_id"])
        for sock in list(registry.by_house.get(house_id, ())):
            if sock.user_id == evicted:
                sock.close_code = 4403
        # fall through: remaining members still get the event

    targets = registry.by_house.get(house_id, set())
    if event_type == "notification":
        target_user = UUID(data["payload"]["user_id"])
        targets = {s for s in targets if s.user_id == target_user}
    for sock in list(targets):
        if sock.close_code is None:
            sock.try_send(text, event_type)


async def _sender(sock: Socket) -> None:
    while sock.close_code is None:
        try:
            raw = await asyncio.wait_for(sock.queue.get(), timeout=0.5)
        except TimeoutError:
            continue
        await sock.ws.send_text(raw)


async def _housekeeper(sock: Socket) -> None:
    """Heartbeat + token-expiry reauth, per WEBSOCKET_PROTOCOL §2.3."""
    last_ping = time.monotonic()
    sock.send_json({"type": "ping"}, "ping")
    while sock.close_code is None:
        await asyncio.sleep(1)
        now = time.monotonic()
        if now - last_ping >= PING_INTERVAL:
            if sock.missed_pongs >= MAX_MISSED_PONGS:
                sock.close_code = 1012
                return
            sock.missed_pongs += 1
            sock.send_json({"type": "ping"}, "ping")
            last_ping = now
        if time.time() > sock.token_exp:
            if sock.reauth_deadline is None:
                sock.reauth_deadline = now + REAUTH_GRACE
                sock.send_json({"type": "reauth"}, "reauth")
            elif now > sock.reauth_deadline:
                sock.close_code = 4401
                return


async def _handle_subscribe(
    sock: Socket, msg: dict[str, Any], factory: async_sessionmaker[AsyncSession]
) -> None:
    try:
        house_id = UUID(msg["house_id"])
    except (KeyError, ValueError):
        sock.send_json({"type": "error", "code": "UNSUPPORTED"}, "error")
        return
    last_seq = msg.get("last_seq")

    async with factory() as session:
        role = await resolve_role(session, sock.user_id, house_id)
        if role is None:
            sock.send_json(
                {"type": "error", "code": "PERMISSION_DENIED", "house_id": str(house_id)},
                "error",
            )
            return
        current_seq: int = (
            await session.execute(
                select(Event.seq)
                .where(Event.house_id == house_id)
                .order_by(Event.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or 0

        if last_seq is not None and current_seq - last_seq > REPLAY_LIMIT:
            # too far behind: client reloads via REST, resubscribes last_seq null
            sock.send_json({"type": "resync", "house_id": str(house_id)}, "resync")
            return

        # live BEFORE replay: no gap window; duplicates dropped by client seq check
        registry.subscribe(sock, house_id)
        if last_seq is not None:
            replay = (
                await session.execute(
                    select(Event)
                    .where(Event.house_id == house_id, Event.seq > last_seq)
                    .order_by(Event.seq)
                )
            ).scalars()
            for event in replay:
                sock.send_json(
                    {
                        "type": "event",
                        "house_id": str(house_id),
                        "seq": event.seq,
                        "event_type": event.type,
                        "ts": event.created_at.isoformat(),
                        "payload": event.payload,
                    },
                    event.type,
                )
    sock.send_json(
        {"type": "subscribed", "house_id": str(house_id), "current_seq": current_seq},
        "subscribed",
    )


async def _handle_typing(
    sock: Socket, msg: dict[str, Any], factory: async_sessionmaker[AsyncSession]
) -> None:
    """Ephemeral, fanned out to the dispute's house, never persisted."""
    try:
        dispute_id = UUID(msg["dispute_id"])
    except (KeyError, ValueError):
        sock.send_json({"type": "error", "code": "UNSUPPORTED"}, "error")
        return
    async with factory() as session:
        dispute = await session.get(Dispute, dispute_id)
    if dispute is None or dispute.house_id not in sock.houses:
        return
    out = json.dumps(
        {"type": "typing", "dispute_id": str(dispute_id), "user_id": str(sock.user_id)}
    )
    for peer in list(registry.by_house.get(dispute.house_id, ())):
        if peer is not sock and peer.close_code is None:
            peer.try_send(out, "typing")


async def _receiver(sock: Socket, factory: async_sessionmaker[AsyncSession]) -> None:
    while sock.close_code is None:
        raw = await sock.ws.receive_text()
        try:
            msg = json.loads(raw)
            mtype = msg.get("type")
        except json.JSONDecodeError:
            mtype = None
            msg = {}
        if mtype == "subscribe":
            await _handle_subscribe(sock, msg, factory)
        elif mtype == "unsubscribe":
            try:
                registry.unsubscribe(sock, UUID(msg["house_id"]))
            except (KeyError, ValueError):
                pass
        elif mtype == "pong":
            sock.missed_pongs = 0
        elif mtype == "auth":
            decoded = decode_token_payload(msg.get("token", ""))
            if decoded and decoded[0] == sock.user_id:
                sock.token_exp = decoded[1]
                sock.reauth_deadline = None
                sock.send_json({"type": "auth.ok"}, "auth.ok")
            else:
                sock.close_code = 4401
        elif mtype == "typing":
            await _handle_typing(sock, msg, factory)
        else:
            # never a disconnect (§5)
            sock.send_json({"type": "error", "code": "UNSUPPORTED"}, "error")


async def handle_socket(
    websocket: WebSocket, factory: async_sessionmaker[AsyncSession]
) -> None:
    await websocket.accept()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), AUTH_TIMEOUT)
        msg = json.loads(raw)
        assert msg.get("type") == "auth"
        decoded = decode_token_payload(msg["token"])
        assert decoded is not None
    except Exception:
        await websocket.close(code=4401)
        return
    user_id, token_exp = decoded

    async with factory() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            await websocket.close(code=4401)
            return
        house_ids = [
            str(h)
            for h in (
                await session.execute(
                    select(HouseMembership.house_id).where(
                        HouseMembership.user_id == user_id,
                        HouseMembership.status == MembershipStatus.active,
                    )
                )
            ).scalars()
        ]

    sock = Socket(websocket, user_id, token_exp)
    registry.add(sock)
    WS_CONNECTIONS_ACTIVE.labels(worker=WORKER).inc()
    await websocket.send_text(json.dumps({"type": "auth.ok", "house_ids": house_ids}))

    tasks = [
        asyncio.create_task(_sender(sock)),
        asyncio.create_task(_housekeeper(sock)),
        asyncio.create_task(_receiver(sock, factory)),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:  # surface receiver/sender crashes other than disconnects
            exc = t.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.warning("ws task error: %r", exc)
    finally:
        for t in tasks:
            t.cancel()
        registry.remove(sock)
        WS_CONNECTIONS_ACTIVE.labels(worker=WORKER).dec()
        with contextlib.suppress(Exception):
            await websocket.close(code=sock.close_code or 1000)


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    from app.db import get_session_factory

    await handle_socket(websocket, get_session_factory())
