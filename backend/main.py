# ============================================================
# GoldTrader Pro — FastAPI Backend
# Taxly India Private Limited
# ============================================================

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
import models  # noqa: F401 — ensures tables are registered

BASE_DIR     = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
INDEX_HTML   = FRONTEND_DIR / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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

# ── Security Headers — pure ASGI (no BaseHTTPMiddleware, no body buffering) ──
# BaseHTTPMiddleware has a Starlette bug where it buffers HTTPException response
# bodies, delivering EMPTY bodies to the client. This causes res.json() to fail
# in the browser → all 401/403/409 error details are silently lost.
# This pure ASGI implementation ONLY modifies the response.start headers event.

class _SecurityHeadersMiddleware:
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

        async def patched_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                have = {h[0].lower() for h in headers}
                for name, value in self._HEADERS:
                    if name not in have:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, patched_send)

app.add_middleware(_SecurityHeadersMiddleware)

# ── CORS ──────────────────────────────────────────────────────────────────────
# RULE: allow_credentials=True is ILLEGAL with allow_origins=["*"].
# This app uses Bearer tokens (Authorization header), not cookies.
# Bearer tokens work with allow_origins=["*"] + allow_credentials=False.

_DEV_ORIGINS = [
    "http://localhost", "http://localhost:3000", "http://localhost:8000",
    "http://localhost:5500", "http://127.0.0.1", "http://127.0.0.1:8000",
    "http://127.0.0.1:5500",
]

_raw  = [o.strip() for o in settings.FRONTEND_URL.split(",") if o.strip()]
_wild = not _raw or _raw == ["*"]

ALLOW_ORIGINS     = ["*"] if _wild else list(dict.fromkeys(_raw + _DEV_ORIGINS))
ALLOW_CREDENTIALS = False if _wild else True

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Action"],
)

# ── Routers ────────────────────────────────────────────────────────────────────
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


# ── Health & Static ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/google-callback.html", response_class=FileResponse)
async def serve_google_callback():
    cb = FRONTEND_DIR / "google-callback.html"
    return FileResponse(str(cb if cb.exists() else INDEX_HTML), media_type="text/html")

@app.get("/", response_class=FileResponse)
async def serve_root():
    return FileResponse(str(INDEX_HTML), media_type="text/html")

@app.get("/{full_path:path}", response_class=FileResponse)
async def serve_frontend(full_path: str):
    return FileResponse(str(INDEX_HTML), media_type="text/html")
