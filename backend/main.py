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

# ── CORS Configuration ───────────────────────────────────────────────────────
# Supports three modes set via FRONTEND_URL in .env:
#
#   FRONTEND_URL=*                          → wildcard (dev/local; no credentials)
#   FRONTEND_URL=http://localhost:3000      → explicit single origin (credentials OK)
#   FRONTEND_URL=https://app.example.com,https://www.example.com
#                                           → multiple origins (credentials OK)
#
# When FRONTEND_URL=* the middleware uses allow_origins=["*"] which the browser
# accepts for Authorization-header requests (Bearer tokens are not cookies, so
# the browser does NOT enforce the credentials restriction on them).
# The allow_credentials=False with wildcard is correct and intentional.
_raw_origins = [o.strip() for o in settings.FRONTEND_URL.split(",") if o.strip()]
_wildcard    = not _raw_origins or _raw_origins == ["*"]
ALLOW_ORIGINS = ["*"] if _wildcard else _raw_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=not _wildcard,   # True only when explicit origins are set
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Action"],       # expose custom headers used in auth responses
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


@app.get("/api/config")
async def frontend_config():
    """
    Public endpoint — returns non-secret runtime configuration for the frontend.
    The frontend fetches this on load to confirm the backend is reachable and
    to receive the correct Google Client ID for OAuth flows.
    """
    return {
        "status":           "ok",
        "app_name":         settings.APP_NAME,
        "google_client_id": settings.GOOGLE_CLIENT_ID or "",
        "trial_days":       settings.TRIAL_DAYS,
    }

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
