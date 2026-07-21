import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.auth import me_router
from app.auth import router as auth_router
from app.config import DEV_JWT_SECRET, get_settings
from app.db import dispose_engine, get_engine, get_session_factory
from app.disputes import router as disputes_router
from app.errors import install_error_handlers
from app.expenses import router as expenses_router
from app.houses import router as houses_router
from app.idempotency import IdempotencyMiddleware
from app.outbox import relay_loop
from app.recurring_bills import router as recurring_bills_router
from app.redis import close_redis, get_redis
from app.settlements import router as settlements_router
from app.split_rules import router as split_rules_router
from app.ws import backplane_consumer
from app.ws import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.env == "production" and settings.jwt_secret == DEV_JWT_SECRET:
        raise RuntimeError("JWT_SECRET must be set in production")
    stop = asyncio.Event()
    background = [
        asyncio.create_task(relay_loop(get_session_factory(), get_redis(), stop)),
        asyncio.create_task(backplane_consumer(get_redis(), stop)),
    ]
    yield
    stop.set()
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(title="SplitLedger", lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(IdempotencyMiddleware)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(me_router, prefix="/api/v1")
    app.include_router(houses_router, prefix="/api/v1")
    app.include_router(split_rules_router, prefix="/api/v1")
    app.include_router(expenses_router, prefix="/api/v1")
    app.include_router(disputes_router, prefix="/api/v1")
    app.include_router(recurring_bills_router, prefix="/api/v1")
    app.include_router(settlements_router, prefix="/api/v1")
    app.include_router(ws_router, prefix="/api/v1")

    @app.get("/api/v1/healthz")
    async def healthz() -> JSONResponse:
        db_ok = False
        redis_ok = False
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            pass
        try:
            await get_redis().ping()
            redis_ok = True
        except Exception:
            pass
        healthy = db_ok and redis_ok
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded", "db": db_ok, "redis": redis_ok},
        )

    return app


app = create_app()
