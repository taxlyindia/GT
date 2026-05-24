# config.py — Application settings loaded from environment variables

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Database (REQUIRED) ───────────────────────────────────
    # Supports postgresql+asyncpg://, postgresql://, or postgres:// (auto-converted)
    DATABASE_URL: str = "postgresql+asyncpg://gt:cms123@localhost:5432/gt"

    # ── Security (REQUIRED in production) ────────────────────
    JWT_SECRET: str = "change-me-generate-with-secrets-token-hex-32"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days

    # ── Taxly Super-Admin ─────────────────────────────────────
    TAXLY_ADMIN_USERNAME: str = "Taxly"
    # Default hash is for password: @Gsf025@
    # Regenerate: python -c "import bcrypt; print(bcrypt.hashpw(b'@Gsf025@', bcrypt.gensalt()).decode())"
    ADMIN_PASSWORD_HASH: str = "$2b$12$ZhJuQ.tZKyxLgVT/GqrzBeX20BFpN0sFKhzMBPUW0HbtnYKR8Mlsi"

    # ── CORS ──────────────────────────────────────────────────
    # Set to your domain: https://yourdomain.com
    # Comma-separate for multiple: https://yourdomain.com,https://www.yourdomain.com
    # Leave as * only for local development (disables credentials in CORS)
    FRONTEND_URL: str = "*"

    # ── Google OAuth (optional) ───────────────────────────────
    GOOGLE_CLIENT_ID: str = "863366877040-e8bi45pfb3mo1ec03c395g8dbaatvke5.apps.googleusercontent.com"
    GOOGLE_CLIENT_SECRET: str = "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"

    # ── Trial ────────────────────────────────────────────────
    TRIAL_DAYS: int = 10

    # ── Email (optional) ─────────────────────────────────────
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    FROM_EMAIL: str = "GoldTrader Pro <support@goldtraderpro.in>"

    # ── S3 Storage (optional) ────────────────────────────────
    S3_BUCKET: str = ""
    S3_ENDPOINT: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""

    # ── Redis (optional) ─────────────────────────────────────
    REDIS_URL: str = ""

    # ── Misc ─────────────────────────────────────────────────
    DEBUG: bool = False
    APP_NAME: str = "GoldTrader Pro"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Don't crash on unknown env vars


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# SEC-02: Warn loudly if JWT secret is left at the insecure default
import sys as _sys
_INSECURE_DEFAULTS = {
    "change-me-generate-with-secrets-token-hex-32",
    "replace-with-a-32-char-random-string-here",
}
if settings.JWT_SECRET in _INSECURE_DEFAULTS:
    print(
        "[SECURITY WARNING] JWT_SECRET is set to an insecure default value. "        "Generate a proper secret: python -c 'import secrets; print(secrets.token_hex(32))'",
        file=_sys.stderr,
    )

