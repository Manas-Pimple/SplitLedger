import httpx

from app.main import create_app


async def test_healthz_reports_db_and_redis_status() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/healthz")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert set(body) == {"status", "db", "redis"}
    assert isinstance(body["db"], bool)
    assert isinstance(body["redis"], bool)
    if resp.status_code == 200:
        assert body == {"status": "ok", "db": True, "redis": True}
    else:
        assert body["status"] == "degraded"
        assert not (body["db"] and body["redis"])
