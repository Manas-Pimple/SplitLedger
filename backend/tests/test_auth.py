from uuid import uuid4

import httpx


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


async def _register(
    client: httpx.AsyncClient, email: str, password: str = "hunter2!"
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": "Tess"},
        headers=_key(),
    )


async def _login(
    client: httpx.AsyncClient, email: str, password: str = "hunter2!"
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, headers=_key()
    )


async def test_register_login_me_flow(client: httpx.AsyncClient) -> None:
    email = f"{uuid4().hex[:10]}@example.com"
    r = await _register(client, email)
    assert r.status_code == 201, r.text
    assert r.json()["email"] == email

    r = await _login(client, email)
    assert r.status_code == 200
    tokens = r.json()

    r = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == email
    assert body["memberships"] == []


async def test_duplicate_email_409(client: httpx.AsyncClient) -> None:
    email = f"{uuid4().hex[:10]}@example.com"
    assert (await _register(client, email)).status_code == 201
    r = await _register(client, email)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CONFLICT"


async def test_wrong_password_401(client: httpx.AsyncClient) -> None:
    email = f"{uuid4().hex[:10]}@example.com"
    await _register(client, email)
    r = await _login(client, email, "wrong")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_refresh_rotation_single_use(client: httpx.AsyncClient) -> None:
    email = f"{uuid4().hex[:10]}@example.com"
    await _register(client, email)
    tokens = (await _login(client, email)).json()

    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
        headers=_key(),
    )
    assert r.status_code == 200
    new_tokens = r.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    # old refresh token is spent
    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
        headers=_key(),
    )
    assert r.status_code == 401


async def test_logout_revokes_refresh(client: httpx.AsyncClient) -> None:
    email = f"{uuid4().hex[:10]}@example.com"
    await _register(client, email)
    tokens = (await _login(client, email)).json()

    r = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=_key(),
    )
    assert r.status_code == 204

    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
        headers=_key(),
    )
    assert r.status_code == 401


async def test_me_requires_token(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/me")
    assert r.status_code == 401
    r = await client.get("/api/v1/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
