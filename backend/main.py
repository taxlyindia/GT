# ============================================================
# GoldTrader Pro v4 — FastAPI Backend
# Taxly India Private Limited
# ============================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
import os

from database import engine, Base
from sqlalchemy import text
from config import settings
from routers import (
    auth, invoices, customers, payments,
    cash, advances, stock, reports, admin, export,
    suppliers,
)
import models  # noqa: F401 — ensures tables are registered

# Path to frontend index.html
# __file__ is always backend/main.py → parent is backend/ → parent is project root
BASE_DIR     = Path(__file__).resolve().parent          # backend/
FRONTEND_DIR = BASE_DIR.parent / "frontend"             # project-root/frontend/
INDEX_HTML   = FRONTEND_DIR / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        # Create all tables from models (new installs)
        await conn.run_sync(Base.metadata.create_all)

        # ── Auto-migrations: add new columns safely ──────────────
        # polish_charges on invoice_items (added in v4.2)
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'invoice_items'
                      AND column_name = 'polish_charges'
                ) THEN
                    ALTER TABLE invoice_items
                        ADD COLUMN polish_charges NUMERIC(15, 2) NOT NULL DEFAULT 0;
                END IF;
            END $$;
        """))

        # Extra tenant profile columns (added in v4.3)
        await conn.execute(text("""
            DO $$
            DECLARE col TEXT;
            BEGIN
                FOREACH col IN ARRAY ARRAY[
                    'pan', 'upi_id', 'qr_code_url', 'bank_name',
                    'bank_account_no', 'bank_ifsc', 'bank_branch',
                    'terms_conditions', 'authorised_person'
                ] LOOP
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'tenants' AND column_name = col
                    ) THEN
                        EXECUTE format(
                            'ALTER TABLE tenants ADD COLUMN %I TEXT', col
                        );
                    END IF;
                END LOOP;
            END $$;
        """))

    yield
    await engine.dispose()


app = FastAPI(
    title="GoldTrader Pro API",
    description="Complete jewellery business management — GST, TCS, SFT, FIFO",
    version="4.1.0",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

# SEC-04 FIX: Security headers via pure ASGI middleware (NOT BaseHTTPMiddleware).
#
# CRITICAL: BaseHTTPMiddleware has a known Starlette/FastAPI bug where it buffers
# the entire response body. For HTTPException error responses this causes the body
# to be consumed by the middleware and delivered as EMPTY to the client.
# Effect: res.json() fails in the browser → data={} → detail=undefined →
#         all error messages (401 "Invalid credentials", 403, etc.) are LOST.
# 
# Pure ASGI middleware injects headers into the http.response.start event only,
# never touching the response body — no buffering, no body corruption.

class _SecurityHeadersMiddleware:
    """Pure ASGI middleware — injects security headers without touching the body."""

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
                # Append security headers to the response start message
                headers = list(message.get("headers", []))
                existing = {h[0].lower() for h in headers}
                for name, value in self._HEADERS:
                    if name not in existing:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(_SecurityHeadersMiddleware)

# Build allowed-origins list from FRONTEND_URL env var.
# Using allow_origins=["*"] with allow_credentials=True is rejected by browsers —
# you must list explicit origins when sending cookies / Authorization headers.
# Set FRONTEND_URL=https://yourdomain.com in your .env (comma-separate for multiple).
# ── CORS setup ────────────────────────────────────────────────────────────
# RULE: allow_credentials=True is INCOMPATIBLE with allow_origins=["*"].
# Browsers reject such responses with a CORS error (shows as HTTP 403 in the
# frontend because the response body is not JSON → data={} → empty detail).
#
# Safe strategy:
#  - If FRONTEND_URL is "*" (wildcard) or not set → use ["*"] but NO credentials.
#    Public API mode: no cookies/auth headers from cross-origin. Tokens sent as
#    Bearer headers still work because CORSMiddleware passes them through.
#  - If FRONTEND_URL lists explicit origins → use those + allow_credentials=True.
#
# Always include localhost variants for development convenience.
# Development origins always included for convenience
_DEV_ORIGINS = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8000",
]

_raw_origins = [o.strip() for o in settings.FRONTEND_URL.split(",") if o.strip()]
_is_wildcard = not _raw_origins or _raw_origins == ["*"]

# CORS RULE (browsers enforce this strictly):
#   allow_credentials=True is INCOMPATIBLE with allow_origins=["*"].
#   Pairing them causes the browser to reject ALL responses — showing as 403/400
#   with an empty body (res.json() returns {} → detail=undefined → fallback msg fires).
#
# This app uses Bearer token authentication (Authorization header), NOT cookies.
# Bearer tokens work correctly with allow_origins=["*"] and allow_credentials=False
# because they are explicit headers, not automatic browser credentials.
# Therefore wildcard mode is safe and fully functional for this app.
if _is_wildcard:
    ALLOW_ORIGINS     = ["*"]
    ALLOW_CREDENTIALS = False    # MUST be False with ["*"] — browser requirement
else:
    # Explicit-origin mode: add dev origins for local development convenience
    ALLOW_ORIGINS     = list(dict.fromkeys(_raw_origins + _DEV_ORIGINS))
    ALLOW_CREDENTIALS = True     # Safe — explicit origins listed

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Action"],
)

# ── API Routers ───────────────────────────────────────────────
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


# ── Frontend: serve index.html for all non-API routes ─────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/google-callback.html", response_class=FileResponse)
async def serve_google_callback():
    """Serve the Google OAuth2 callback page for the popup flow."""
    cb = FRONTEND_DIR / "google-callback.html"
    if cb.exists():
        return FileResponse(str(cb), media_type="text/html")
    return FileResponse(str(INDEX_HTML), media_type="text/html")

@app.get("/", response_class=FileResponse)
async def serve_root():
    """Serve the frontend SPA."""
    return FileResponse(str(INDEX_HTML), media_type="text/html")

@app.get("/{full_path:path}", response_class=FileResponse)
async def serve_frontend(full_path: str):
    """Catch-all: serve index.html for any non-API path (SPA routing)."""
    return FileResponse(str(INDEX_HTML), media_type="text/html")
