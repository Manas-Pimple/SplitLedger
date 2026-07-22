from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-only-secret-override-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://splitledger:splitledger@localhost:5434/splitledger"
    redis_url: str = "redis://localhost:6380/0"
    env: str = "development"
    jwt_secret: str = DEV_JWT_SECRET

    # R2 in production, MinIO locally — same S3-compatible API, config-only swap
    r2_endpoint_url: str = "http://localhost:9002"
    r2_access_key_id: str = "splitledger"
    r2_secret_access_key: str = "splitledger123"
    r2_bucket: str = "splitledger-dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
