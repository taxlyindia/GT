# config.py — Application settings loaded from environment variables
# ─────────────────────────────────────────────────────────────────
# GoldTrader Pro — Hosted on Hostinger VPS (Ubuntu 22.04)
#
# HOW TO CONFIGURE:
#   sudo mkdir -p /etc/goldtrader
#   sudo cp backend/.env.example /etc/goldtrader/.env
#   sudo nano /etc/goldtrader/.env   ← fill in real values
#   sudo systemctl restart goldtrader
#
# NEVER commit real secrets to git.
# ─────────────────────────────────────────────────────────────────

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Database (REQUIRED) ───────────────────────────────────
    # Replace with your actual PostgreSQL credentials.
    # Format: postgresql+asyncpg://USER:PASSWORD@localhost:5432/DBNAME
    DATABASE_URL: str = "postgresql+asyncpg://goldtrader:password@localhost:5432/goldtrader_db"

    # ── Security (REQUIRED in production) ────────────────────
    # Generate: python3 -c "import secrets; print(secrets.token_hex(32))"
    JWT_SECRET: str = "change-me-generate-with-secrets-token-hex-32"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days

    # ── Taxly Super-Admin ─────────────────────────────────────
    TAXLY_ADMIN_USERNAME: str = "Taxly"
    # Default hash is for password: @Gsf025@
    # To change: python3 -c "import bcrypt; print(bcrypt.hashpw(b'NewPass', bcrypt.gensalt()).decode())"
    ADMIN_PASSWORD_HASH: str = "$2b$12$ZhJuQ.tZKyxLgVT/GqrzBeX20BFpN0sFKhzMBPUW0HbtnYKR8Mlsi"

    # ── CORS ──────────────────────────────────────────────────
    # Set to your domain: https://yourdomain.com
    # For multiple: https://yourdomain.com,https://www.yourdomain.com
    # Leave as * only for local dev (disables credentials in CORS)
    FRONTEND_URL: str = "*"

    # ── Google OAuth ──────────────────────────────────────────
    # BUG-FIX [SEC-01]: Secrets must NEVER be hardcoded here.
    # Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in /etc/goldtrader/.env
    # Get values from: https://console.cloud.google.com → APIs → Credentials
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

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
        env_file = "/etc/goldtrader/.env"   # Hostinger VPS env file location
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# ── Startup warnings ──────────────────────────────────────────
import sys as _sys

_INSECURE_JWT = {
    "change-me-generate-with-secrets-token-hex-32",
    "replace-with-a-32-char-random-string-here",
    "",
}
if settings.JWT_SECRET in _INSECURE_JWT:
    print(
        "[SECURITY WARNING] JWT_SECRET is not configured!\n"
        "  Generate one: python3 -c 'import secrets; print(secrets.token_hex(32))'\n"
        "  Then add to /etc/goldtrader/.env: JWT_SECRET=<generated_value>\n"
        "  Restart: sudo systemctl restart goldtrader",
        file=_sys.stderr,
    )

if not settings.GOOGLE_CLIENT_ID:
    print(
        "[INFO] GOOGLE_CLIENT_ID is not set — Google Sign-In will run in unverified demo mode.\n"
        "  For production: set GOOGLE_CLIENT_ID in /etc/goldtrader/.env",
        file=_sys.stderr,
    )

if not settings.GOOGLE_CLIENT_SECRET:
    print(
        "[INFO] GOOGLE_CLIENT_SECRET is not set — Google OAuth callback (/api/auth/google/callback) will fail.\n"
        "  For production: set GOOGLE_CLIENT_SECRET in /etc/goldtrader/.env",
        file=_sys.stderr,
    )
