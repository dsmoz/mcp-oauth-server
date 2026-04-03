from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    OAUTH_ISSUER_URL: str = "http://localhost:8000"
    INTROSPECT_SECRET: str = "change-me"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"
    ACCESS_TOKEN_TTL: int = 3600
    REFRESH_TOKEN_TTL: int = 2592000
    PORT: int = 8000
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_OWNER_CHAT_ID: str = ""
    BREVO_API_KEY: str = ""
    BREVO_SENDER_EMAIL: str = ""
    BREVO_SENDER_NAME: str = "DS-MOZ Intelligence"
    SECRET_KEY: str = "change-me-portal-secret"
    RAILWAY_API_TOKEN: str = ""
    RAILWAY_PROJECT_ID: str = ""


@lru_cache()
def get_settings() -> Settings:
    return Settings()
