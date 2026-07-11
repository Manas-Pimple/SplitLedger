from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-only-secret-override-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://splitledger:splitledger@localhost:5434/splitledger"
    redis_url: str = "redis://localhost:6380/0"
    env: str = "development"
    jwt_secret: str = DEV_JWT_SECRET


@lru_cache
def get_settings() -> Settings:
    return Settings()
