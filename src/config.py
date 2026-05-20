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
    RESEND_API_KEY: str = ""
    RESEND_SENDER_EMAIL: str = "noreply@send.dsmozconsultancy.com"
    RESEND_SENDER_NAME: str = "DS-MOZ Connect"
    SECRET_KEY: str = "change-me-portal-secret"
    RAILWAY_API_TOKEN: str = ""
    RAILWAY_PROJECT_ID: str = ""
    RAILWAY_PROJECT_IDS: str = ""  # Comma-separated; overrides RAILWAY_PROJECT_ID when set
    ANTHROPIC_API_KEY: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    # Social sign-in (Google + Microsoft). Buttons hidden when client_id blank.
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    MICROSOFT_OAUTH_CLIENT_ID: str = ""
    MICROSOFT_OAUTH_CLIENT_SECRET: str = ""
    MICROSOFT_OAUTH_TENANT: str = "common"
    # Fernet key (url-safe base64, 32 bytes) used to encrypt MS Graph refresh
    # tokens at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    GRAPH_TOKEN_ENCRYPTION_KEY: str = ""


@lru_cache()
def get_settings() -> Settings:
    return Settings()
