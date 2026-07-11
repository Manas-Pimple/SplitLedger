"""Idempotency-Key middleware per API_SPEC §1: every POST/PATCH/DELETE under
/api/v1 requires the header. Stored response is replayed for a repeated key;
same key with a different request → 409 IDEMPOTENCY_KEY_REUSED.

Pure ASGI (not BaseHTTPMiddleware) so the request body can be captured and
replayed reliably. Only JSON responses are snapshotted — all API responses
are JSON per spec.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db import get_session_factory
from app.models import IdempotencyKey
from app.security import decode_access_token

MUTATING = {"POST", "PATCH", "DELETE"}
TTL = timedelta(hours=24)


async def _send_json(send: Send, status: int, body_obj: Any) -> None:
    body = json.dumps(body_obj).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in MUTATING:
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if not path.startswith("/api/v1"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        key = headers.get("idempotency-key")
        if not key:
            await _send_json(
                send,
                400,
                {
                    "error": {
                        "code": "IDEMPOTENCY_KEY_REQUIRED",
                        "message": "Idempotency-Key header required",
                        "details": {},
                    }
                },
            )
            return

        # Drain the request body so it can be hashed, then replayed downstream
        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)

        # Best-effort caller identity (header may be absent on auth endpoints)
        auth = headers.get("authorization", "")
        user_id = (
            decode_access_token(auth.removeprefix("Bearer "))
            if auth.startswith("Bearer ")
            else None
        )
        request_hash = hashlib.sha256(
            f"{scope['method']}|{path}|{user_id}|".encode() + body
        ).hexdigest()

        factory = get_session_factory()
        async with factory() as session:
            stored = (
                await session.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
            ).scalar_one_or_none()
            if stored is not None and stored.expires_at > datetime.now(UTC):
                # user_id is baked into request_hash, so one comparison covers both
                if stored.request_hash != request_hash:
                    await _send_json(
                        send,
                        409,
                        {
                            "error": {
                                "code": "IDEMPOTENCY_KEY_REUSED",
                                "message": "Key was used with a different request",
                                "details": {},
                            }
                        },
                    )
                    return
                if stored.response_status is not None:
                    if stored.response_body is None:
                        # empty/non-JSON original (e.g. 204 logout): no body on replay
                        await send(
                            {
                                "type": "http.response.start",
                                "status": stored.response_status,
                                "headers": [],
                            }
                        )
                        await send({"type": "http.response.body", "body": b""})
                    else:
                        await _send_json(send, stored.response_status, stored.response_body)
                    return
                # ponytail: in-flight duplicate (row claimed, no response yet) —
                # fall through and process again; last write wins on snapshot.

        async def replay_receive() -> Message:
            return {"type": "http.request", "body": body, "more_body": False}

        # Capture the downstream response to snapshot it
        response: dict[str, Any] = {"status": None, "body": b""}

        async def capture_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
            elif message["type"] == "http.response.body":
                response["body"] += message.get("body", b"")
            await send(message)

        await self.app(scope, replay_receive, capture_send)

        try:
            response_body = json.loads(response["body"]) if response["body"] else None
        except json.JSONDecodeError:
            response_body = None
        async with factory() as session:
            await session.execute(
                pg_insert(IdempotencyKey)
                .values(
                    key=key,
                    user_id=user_id,
                    request_hash=request_hash,
                    response_status=response["status"],
                    response_body=response_body,
                    expires_at=datetime.now(UTC) + TTL,
                )
                .on_conflict_do_update(
                    index_elements=[IdempotencyKey.key],
                    set_={
                        "response_status": response["status"],
                        "response_body": response_body,
                        "expires_at": datetime.now(UTC) + TTL,
                    },
                )
            )
            await session.commit()
