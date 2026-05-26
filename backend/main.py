# ============================================================
# GoldTrader Pro — FastAPI Backend  v4.1.2
# Taxly India Private Limited
# Hosted on: Hostinger VPS (Ubuntu 22.04)
# ============================================================

import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from contextlib import asynccontextmanager
from pathlib import Path

from database import engine, Base
from sqlalchemy import text
from config import settings
from routers import (
    auth, invoices, customers, payments,
    cash, advances, stock, reports, admin, export,
    suppliers,
)
import models  # noqa: F401

# ── Logging setup ─────────────────────────────────────────────────────────────
# Logs go to stdout — captured by systemd journal on Hostinger VPS.
# View live: sudo journalctl -u goldtrader -f
# View last 100 lines: sudo journalctl -u goldtrader -n 100
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("gt")

BASE_DIR     = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
INDEX_HTML   = FRONTEND_DIR / "index.html"


# ── DB startup ────────────────────────────────────────────────────────────────
# Track DB health so /health and /api/diagnose can report it accurately
_db_ready: bool = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db_ready
    logger.info("Starting up GoldTrader Pro v4.1.2...")

    # Validate DATABASE_URL is configured for production
    from config import settings as _s
    if not _s.DATABASE_URL or _s.DATABASE_URL == "postgresql+asyncpg://gt:cms123@localhost:5432/gt":
        logger.warning(
            "DATABASE_URL is using the default value. "
            "Edit /etc/goldtrader/.env on your Hostinger VPS and set "
            "DATABASE_URL to your actual PostgreSQL connection string."
        )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            await conn.execute(text("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='invoice_items' AND column_name='polish_charges'
                    ) THEN
                        ALTER TABLE invoice_items
                            ADD COLUMN polish_charges NUMERIC(15,2) NOT NULL DEFAULT 0;
                    END IF;
                END $$;
            """))

            await conn.execute(text("""
                DO $$ DECLARE col TEXT; BEGIN
                    FOREACH col IN ARRAY ARRAY[
                        'pan','upi_id','qr_code_url','bank_name',
                        'bank_account_no','bank_ifsc','bank_branch',
                        'terms_conditions','authorised_person'
                    ] LOOP
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name='tenants' AND column_name=col
                        ) THEN
                            EXECUTE format('ALTER TABLE tenants ADD COLUMN %I TEXT', col);
                        END IF;
                    END LOOP;
                END $$;
            """))

        _db_ready = True
        logger.info("Database ready. GoldTrader Pro is accepting requests.")
    except Exception as e:
        _db_ready = False
        logger.error(
            f"DB startup error: {e}\n"
            "CHECK: Is PostgreSQL running on the VPS?  →  sudo systemctl status postgresql\n"
            "CHECK: Is DATABASE_URL correct in /etc/goldtrader/.env ?\n"
            "CHECK: Does the database user have CONNECT + USAGE permissions?\n"
            "RUN:   sudo -u postgres psql -c '\\l'  to list databases"
        )
        # Do NOT raise — let the app start so /health returns a useful error
        # instead of nginx returning a bare 502 with no body.

    yield
    await engine.dispose()
    logger.info("Shutdown complete.")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="GoldTrader Pro API",
    description="Jewellery business management",
    version="4.1.2",
    lifespan=lifespan,
    redirect_slashes=False,   # CRITICAL: prevent 307 redirects that convert POST→GET
)


# ── Request logger middleware ─────────────────────────────────────────────────
# Logs every request + response status.
# View on Hostinger VPS: sudo journalctl -u goldtrader -f

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"→ {request.method} {request.url.path}  origin={request.headers.get('origin','same-origin')}")
    try:
        response = await call_next(request)
        logger.info(f"← {response.status_code} {request.method} {request.url.path}")
        return response
    except Exception as e:
        logger.error(f"✗ {request.method} {request.url.path} — {e}")
        raise


# ── Security Headers — pure ASGI, no body buffering ──────────────────────────
#
# DO NOT use BaseHTTPMiddleware here. It has a critical Starlette bug:
# BaseHTTPMiddleware buffers HTTPException response bodies, causing the body
# to be delivered EMPTY to clients. This makes all 401/403/409 error details
# invisible to the frontend — res.json() returns {} → detail=undefined.
#
# This pure ASGI implementation only touches http.response.start (headers).
# The body passes through completely untouched.

class SecurityHeadersMiddleware:
    _HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options",        b"DENY"),
        (b"x-xss-protection",       b"1; mode=block"),
        (b"referrer-policy",        b"strict-origin-when-cross-origin"),
        (b"permissions-policy",     b"geolocation=(), microphone=(), camera=()"),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {h[0].lower() for h in headers}
                for name, val in self._HEADERS:
                    if name not in existing:
                        headers.append((name, val))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


# ── CORS ──────────────────────────────────────────────────────────────────────
# Bearer token auth does NOT need allow_credentials=True.
# allow_credentials=True + allow_origins=["*"] is ILLEGAL (browser rejects it).

_DEV_ORIGINS = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:8000",
    "http://localhost:5500",
    "http://127.0.0.1",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:5500",
]

_configured = [o.strip() for o in settings.FRONTEND_URL.split(",") if o.strip()]
_wildcard   = not _configured or _configured == ["*"]

if _wildcard:
    _ALLOW_ORIGINS     = ["*"]
    _ALLOW_CREDENTIALS = False
else:
    _ALLOW_ORIGINS     = list(dict.fromkeys(_configured + _DEV_ORIGINS))
    _ALLOW_CREDENTIALS = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOW_ORIGINS,
    allow_credentials=_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Action"],
)

logger.info(f"CORS origins: {_ALLOW_ORIGINS}")
logger.info(f"CORS credentials: {_ALLOW_CREDENTIALS}")


# ── Health & Diagnostic routes ────────────────────────────────────────────────
# Registered FIRST — before all API routers and the catch-all static route.
# This ensures they are never intercepted by the catch-all /{full_path:path}.

@app.get("/health", tags=["System"])
async def health_check():
    """
    Health check endpoint — used by nginx upstream checks and monitoring tools.
    Returns db_ready=false with instructions if the database is not connected.
    Always returns HTTP 200 so the process stays up and nginx can report the error.
    """
    if _db_ready:
        return {"status": "ok", "version": "4.1.2", "db_ready": True}
    else:
        return {
            "status": "degraded",
            "version": "4.1.2",
            "db_ready": False,
            "error": "Database not connected. Check DATABASE_URL in /etc/goldtrader/.env",
            "fix": (
                "On your Hostinger VPS, run: sudo nano /etc/goldtrader/.env\n"
                "Set DATABASE_URL=postgresql+asyncpg://USER:PASS@localhost:5432/DBNAME\n"
                "Then restart: sudo systemctl restart goldtrader"
            ),
        }


@app.get("/api/ping", tags=["System"])
async def api_ping_get():
    """
    GET diagnostic. If this returns 200, the app is running and GET routes work.
    If nginx returns 502 here, the uvicorn process is not running.
    Check: sudo systemctl status goldtrader
    """
    return {"pong": True, "method": "GET", "version": "4.1.2"}


@app.post("/api/ping", tags=["System"])
async def api_ping_post():
    """
    POST diagnostic. If GET /api/ping works but this returns 400/403,
    POST requests are being blocked by nginx or a firewall rule.
    Check: sudo nginx -t  and  sudo ufw status
    """
    return {"pong": True, "method": "POST", "version": "4.1.2"}


@app.get("/api/diagnose", tags=["System"])
async def diagnose():
    """
    Deployment diagnostic — open in browser to debug login issues.
    Returns the current state of the server, database, and configuration.
    Safe to expose publicly (no secrets returned).
    """
    from config import settings as _s
    db_url_safe = "NOT SET" if not _s.DATABASE_URL else (
        "localhost default (not configured)" if _s.DATABASE_URL == "postgresql+asyncpg://gt:cms123@localhost:5432/gt"
        else "configured ✓"
    )
    jwt_ok    = _s.JWT_SECRET not in {"change-me-generate-with-secrets-token-hex-32", "replace-with-a-32-char-random-string-here", ""}
    google_ok = bool(_s.GOOGLE_CLIENT_ID and _s.GOOGLE_CLIENT_SECRET)

    issues = []
    if not _db_ready:
        issues.append(
            "DATABASE_URL not connected — "
            "check /etc/goldtrader/.env and run: sudo systemctl status postgresql"
        )
    if not jwt_ok:
        issues.append(
            "JWT_SECRET is using the insecure default — "
            "generate one: python3 -c \"import secrets; print(secrets.token_hex(32))\""
            " and set it in /etc/goldtrader/.env"
        )
    if not google_ok:
        issues.append(
            "Google OAuth not configured — "
            "set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in /etc/goldtrader/.env"
        )

    return {
        "app":          "GoldTrader Pro v4.1.2",
        "host":         "Hostinger VPS",
        "status":       "ok" if _db_ready else "degraded — database not connected",
        "db_ready":     _db_ready,
        "db_url":       db_url_safe,
        "jwt_secret":   "configured ✓" if jwt_ok else "⚠ using insecure default",
        "google_oauth": "configured ✓" if google_ok else "⚠ not configured (Google Sign-In disabled)",
        "issues":       issues if issues else ["none — all systems operational ✓"],
        "next_step": (
            "Edit /etc/goldtrader/.env → set DATABASE_URL → sudo systemctl restart goldtrader"
            if not _db_ready else "All good — login should work."
        ),
    }


# ── API Routers ───────────────────────────────────────────────────────────────
app.include_router(auth.router,       prefix="/api/auth",       tags=["Auth"])
app.include_router(invoices.router,   prefix="/api/invoices",   tags=["Invoices"])
app.include_router(customers.router,  prefix="/api/customers",  tags=["Customers"])
app.include_router(payments.router,   prefix="/api/payments",   tags=["Payments"])
app.include_router(cash.router,       prefix="/api/cash",       tags=["Cash Register"])
app.include_router(advances.router,   prefix="/api/advances",   tags=["Advances"])
app.include_router(stock.router,      prefix="/api/stock",      tags=["Stock"])
app.include_router(reports.router,    prefix="/api/reports",    tags=["Reports"])
app.include_router(export.router,     prefix="/api/export",     tags=["Export"])
app.include_router(admin.router,      prefix="/api/admin",      tags=["Admin"])
app.include_router(suppliers.router,  prefix="/api/suppliers",  tags=["Suppliers"])


# ── SPA / Static Frontend ─────────────────────────────────────────────────────
# These catch-all routes MUST be registered LAST.
# All /api/* requests are handled by the routers above and never reach here.

@app.get("/google-callback.html", response_class=FileResponse, tags=["Static"])
async def serve_google_callback():
    cb = FRONTEND_DIR / "google-callback.html"
    return FileResponse(str(cb if cb.exists() else INDEX_HTML), media_type="text/html")


@app.get("/", response_class=FileResponse, tags=["Static"])
async def serve_root():
    return FileResponse(str(INDEX_HTML), media_type="text/html")


@app.get("/{full_path:path}", response_class=FileResponse, tags=["Static"])
async def serve_frontend(full_path: str):
    """Serves index.html for all non-API frontend routes (SPA routing)."""
    return FileResponse(str(INDEX_HTML), media_type="text/html")
