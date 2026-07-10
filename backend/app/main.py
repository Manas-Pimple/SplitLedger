from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import dispose_engine, get_engine
from app.redis import close_redis, get_redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(title="SplitLedger", lifespan=lifespan)

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
