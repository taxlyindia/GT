#!/bin/bash
# start.sh — GoldTrader Pro production startup (Hostinger VPS)
# Called by systemd — do NOT run manually in production.
#
# To control the service:
#   sudo systemctl start goldtrader
#   sudo systemctl stop goldtrader
#   sudo systemctl restart goldtrader
#   sudo systemctl status goldtrader
#   sudo journalctl -u goldtrader -f       ← live logs

set -e

echo "========================================"
echo "  GoldTrader Pro — Starting"
echo "========================================"

# Load env file if running manually (systemd uses EnvironmentFile= directly)
ENV_FILE="/etc/goldtrader/.env"
if [ -f "$ENV_FILE" ]; then
    set -o allexport && source "$ENV_FILE" && set +o allexport
    echo "Loaded: $ENV_FILE"
else
    echo "WARNING: $ENV_FILE not found."
    echo "Run: sudo mkdir -p /etc/goldtrader && sudo cp .env.example /etc/goldtrader/.env"
fi

# Validate DATABASE_URL
if [ -z "$DATABASE_URL" ]; then
    echo "ERROR: DATABASE_URL is not set in $ENV_FILE"
    echo "Add: DATABASE_URL=postgresql+asyncpg://goldtrader:PASSWORD@localhost:5432/goldtrader_db"
    exit 1
fi

# Warn if JWT_SECRET is default
if [ -z "$JWT_SECRET" ] || [ "$JWT_SECRET" = "change-me-generate-with-secrets-token-hex-32" ]; then
    echo "WARNING: JWT_SECRET is insecure. Generate: python3 -c 'import secrets; print(secrets.token_hex(32))'"
fi

# Check PostgreSQL
systemctl is-active --quiet postgresql 2>/dev/null || echo "WARNING: PostgreSQL may not be running — sudo systemctl start postgresql"

export PORT="${PORT:-8000}"
echo "Starting uvicorn on 127.0.0.1:$PORT ..."

# Bind to 127.0.0.1 only — nginx proxies from 80/443
exec uvicorn main:app \
    --host 127.0.0.1 \
    --port "$PORT" \
    --workers 1 \
    --log-level info \
    --access-log \
    --no-server-header
