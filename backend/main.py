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
import models.erp_models  # noqa: F401 — registers ERP tables (v5 ERP upgrade)

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

        # ── ERP Upgrade v5: add audit / versioning columns safely ────────────
        # These are idempotent; safe to run on every startup.
        await conn.execute(text("""
            DO $$
            BEGIN
                -- invoices ERP columns
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='invoices' AND column_name='version_no') THEN
                    ALTER TABLE invoices ADD COLUMN version_no INTEGER NOT NULL DEFAULT 1;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='invoices' AND column_name='cancelled_at') THEN
                    ALTER TABLE invoices ADD COLUMN cancelled_at TIMESTAMPTZ;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='invoices' AND column_name='cancelled_by') THEN
                    ALTER TABLE invoices ADD COLUMN cancelled_by INTEGER;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='invoices' AND column_name='cancellation_reason') THEN
                    ALTER TABLE invoices ADD COLUMN cancellation_reason TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='invoices' AND column_name='reversal_ref_id') THEN
                    ALTER TABLE invoices ADD COLUMN reversal_ref_id INTEGER;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='invoices' AND column_name='original_transaction_id') THEN
                    ALTER TABLE invoices ADD COLUMN original_transaction_id INTEGER;
                END IF;

                -- supplier_invoices ERP columns
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='supplier_invoices' AND column_name='version_no') THEN
                    ALTER TABLE supplier_invoices ADD COLUMN version_no INTEGER NOT NULL DEFAULT 1;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='supplier_invoices' AND column_name='cancelled_at') THEN
                    ALTER TABLE supplier_invoices ADD COLUMN cancelled_at TIMESTAMPTZ;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='supplier_invoices' AND column_name='cancelled_by') THEN
                    ALTER TABLE supplier_invoices ADD COLUMN cancelled_by INTEGER;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='supplier_invoices' AND column_name='cancellation_reason') THEN
                    ALTER TABLE supplier_invoices ADD COLUMN cancellation_reason TEXT;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='supplier_invoices' AND column_name='reversal_ref_id') THEN
                    ALTER TABLE supplier_invoices ADD COLUMN reversal_ref_id INTEGER;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='supplier_invoices' AND column_name='original_transaction_id') THEN
                    ALTER TABLE supplier_invoices ADD COLUMN original_transaction_id INTEGER;
                END IF;

                -- invoice_items
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='invoice_items' AND column_name='version_no') THEN
                    ALTER TABLE invoice_items ADD COLUMN version_no INTEGER NOT NULL DEFAULT 1;
                END IF;

                -- supplier_invoice_items
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='supplier_invoice_items' AND column_name='purchase_txn_id') THEN
                    ALTER TABLE supplier_invoice_items ADD COLUMN purchase_txn_id INTEGER;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='supplier_invoice_items' AND column_name='version_no') THEN
                    ALTER TABLE supplier_invoice_items ADD COLUMN version_no INTEGER NOT NULL DEFAULT 1;
                END IF;

                -- stock_transactions
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='stock_transactions' AND column_name='version_no') THEN
                    ALTER TABLE stock_transactions ADD COLUMN version_no INTEGER NOT NULL DEFAULT 1;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='stock_transactions' AND column_name='original_transaction_id') THEN
                    ALTER TABLE stock_transactions ADD COLUMN original_transaction_id INTEGER;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='stock_transactions' AND column_name='reversal_ref_id') THEN
                    ALTER TABLE stock_transactions ADD COLUMN reversal_ref_id INTEGER;
                END IF;
            END $$;
        """))

    yield
    await engine.dispose()


app = FastAPI(
    title="GoldTrader Pro API",
    description="Complete jewellery business management — GST, TCS, SFT, FIFO",
    version="4.1.0",
    lifespan=lifespan,
    root_path=settings.ROOT_PATH,   # e.g. "/gt" when behind a reverse proxy subpath
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

# Build allowed-origins list from FRONTEND_URL env var.
# Using allow_origins=["*"] with allow_credentials=True is rejected by browsers —
# you must list explicit origins when sending cookies / Authorization headers.
# Set FRONTEND_URL=https://yourdomain.com in your .env (comma-separate for multiple).
_raw_origins = [o.strip() for o in settings.FRONTEND_URL.split(",") if o.strip()]
ALLOW_ORIGINS = _raw_origins if _raw_origins and _raw_origins != ["*"] else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=ALLOW_ORIGINS != ["*"],   # credentials only when origins are explicit
    allow_methods=["*"],
    allow_headers=["*"],
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


# ── Health & Config endpoints ──────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/config")
async def get_config():
    """Frontend connectivity check + runtime config injection."""
    return {
        "status": "ok",
        "app": "GoldTrader Pro",
        "google_client_id": settings.GOOGLE_CLIENT_ID,
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
