"""Done-gate: same key twice → identical response, one DB row.
Same key + different body → 409. Missing key on mutation → 400."""

from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def test_replay_identical_response_one_row(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    key = str(uuid4())
    email = f"{uuid4().hex[:10]}@example.com"
    payload = {"email": email, "password": "hunter2!", "display_name": "Tess"}

    first = await client.post(
        "/api/v1/auth/register", json=payload, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 201
    replay = await client.post(
        "/api/v1/auth/register", json=payload, headers={"Idempotency-Key": key}
    )
    assert replay.status_code == 201
    assert replay.json() == first.json()

    count = (
        await session.execute(select(func.count()).select_from(User).where(User.email == email))
    ).scalar_one()
    assert count == 1


async def test_key_reuse_different_body_409(client: httpx.AsyncClient) -> None:
    key = str(uuid4())
    def payload(name: str) -> dict[str, str]:
        return {
            "email": f"{uuid4().hex[:10]}@example.com",
            "password": "hunter2!",
            "display_name": name,
        }

    await client.post(
        "/api/v1/auth/register", json=payload("A"), headers={"Idempotency-Key": key}
    )
    r = await client.post(
        "/api/v1/auth/register", json=payload("B"), headers={"Idempotency-Key": key}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_missing_key_400(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": "x@example.com", "password": "hunter2!", "display_name": "X"},
    )
    assert r.status_code == 400


async def test_get_requests_need_no_key(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/healthz")
    assert r.status_code in (200, 503)


async def test_replay_of_empty_204_response(client: httpx.AsyncClient) -> None:
    """Regression: replaying a stored empty-body response (logout 204) must not
    emit a `null` JSON body."""
    email = f"{uuid4().hex[:10]}@example.com"
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "hunter2!", "display_name": "T"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    tokens = (
        await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "hunter2!"},
            headers={"Idempotency-Key": str(uuid4())},
        )
    ).json()

    key = str(uuid4())
    first = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Idempotency-Key": key},
    )
    replay = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == replay.status_code == 204
    assert replay.content == b""


async def test_expired_key_row_renews_on_reuse(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Regression: reprocessing an expired key must renew expires_at, not leave
    the row permanently expired."""
    from datetime import UTC, datetime, timedelta

    from app.models import IdempotencyKey

    key = str(uuid4())
    email = f"{uuid4().hex[:10]}@example.com"
    payload = {"email": email, "password": "hunter2!", "display_name": "T"}
    await client.post("/api/v1/auth/register", json=payload, headers={"Idempotency-Key": key})

    row = await session.get(IdempotencyKey, key)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await session.commit()

    # expired → falls through, reprocesses (409 CONFLICT: email exists), renews row
    r = await client.post(
        "/api/v1/auth/register", json=payload, headers={"Idempotency-Key": key}
    )
    assert r.status_code == 409

    session.expire_all()
    row = await session.get(IdempotencyKey, key)
    assert row is not None
    assert row.expires_at > datetime.now(UTC)
