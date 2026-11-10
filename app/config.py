from pydantic import field_validator
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
    # Hard-coded for the client demo. In a real deployment this comes from Azure Key Vault or a CI/CD secret.
    JWT_SECRET_KEY: str = "local-demo-secret-please-replace-in-real-production-env"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 30

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def _reject_default_secret_in_prod(cls, v: str, info) -> str:
        env = (info.data.get("ENVIRONMENT") or "development").lower()
        if env == "production" and v == "local-demo-secret-please-replace-in-real-production-env":
            raise ValueError(
                "JWT_SECRET_KEY must be changed from the default before running in production"
            )
        if env == "production" and len(v) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters in production")
        return v

    # LLM provider — one standard OpenAI SDK path covers every deployment
    # target: api.openai.com (leave OPENAI_BASE_URL empty), an on-prem
    # OpenAI-compatible server (vLLM / LiteLLM / Ollama), or Azure OpenAI via
    # its OpenAI-compatible endpoint URL. Leave the key blank until a real one
    # is provisioned; /api/explain then uses its explicit rule-based mode and
    # /api/agent/chat returns 503.
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_BASE_URL: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
