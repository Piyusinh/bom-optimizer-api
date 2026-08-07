from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite+aiosqlite:///./bom_platform.db"
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "debug"
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:5174"

    # Local / bundled sample-data directory for the "Ingest All Local Files" feature.
    # Leave empty to use the packaged api/app/sample_data/ folder or the repo docs path.
    SAMPLES_DIR: str = ""

    # Auth (Phase 0 mock — replaced by Entra ID / OAuth2 in later phases)
    JWT_SECRET_KEY: str = "local-dev-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 30

    # Azure OpenAI (optional for Phase 0)
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_KEY: str = ""
    AZURE_OPENAI_DEPLOYMENT: str = "gpt-4o"
    AZURE_OPENAI_API_VERSION: str = "2024-08-01-preview"

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
