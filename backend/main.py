# ============================================================
# GoldTrader Pro — FastAPI Backend  v4.1.1
# Taxly India Private Limited
# ============================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
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

BASE_DIR     = Path(__file__).resolve().parent          # .../backend/
FRONTEND_DIR = BASE_DIR.parent / "frontend"             # .../frontend/
INDEX_HTML   = FRONTEND_DIR / "index.html"


# ── DB startup migrations ─────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Add polish_charges column if missing (v4.2 migration)
        await conn.execute(text("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='invoice_items' AND column_name='polish_charges'
                ) THEN
                    ALTER TABLE invoice_items ADD COLUMN polish_charges NUMERIC(15,2) NOT NULL DEFAULT 0;
                END IF;
            END $$;
        """))

        # Add extra tenant profile columns if missing (v4.3 migration)
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

    yield
    await engine.dispose()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="GoldTrader Pro API",
    description="Jewellery business management — GST, TCS, SFT, FIFO",
    version="4.1.1",
    lifespan=lifespan,
)


# ── Security Headers — pure ASGI (safe: never buffers response body) ─────────
#
# WHY NOT BaseHTTPMiddleware:
#   Starlette's BaseHTTPMiddleware buffers the entire HTTPException response body
#   before forwarding it. Small error responses (401/403/409 JSON payloads) are
#   consumed and re-emitted incorrectly, resulting in EMPTY bodies reaching the
#   browser. The browser's res.json() then fails → data={} → detail=undefined →
#   the frontend shows "Request failed (HTTP 4xx)" with no meaningful text.
#
# This pure ASGI class only hooks into http.response.start (the headers event)
# and passes all other events (body chunks) through completely untouched.

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

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {h[0].lower() for h in headers}
                for name, val in self._HEADERS:
                    if name not in existing:
                        headers.append((name, val))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


app.add_middleware(SecurityHeadersMiddleware)


# ── CORS ──────────────────────────────────────────────────────────────────────
#
# RULE: allow_credentials=True is INCOMPATIBLE with allow_origins=["*"].
#   Pairing them causes the browser to reject ALL cross-origin responses
#   with a CORS error — showing as 403 with an empty body.
#
# This app authenticates via Bearer tokens in the Authorization header (not cookies).
# Bearer tokens are explicit headers — they work fine with allow_origins=["*"]
# and allow_credentials=False. No credentials mode needed.
#
# When FRONTEND_URL is set to an explicit domain (production), credentials are
# enabled so future cookie-based features work without code changes.

_DEV_ORIGINS = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:8000",
    "http://localhost:5500",   # VS Code Live Server
    "http://127.0.0.1",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:5500",
]

_configured = [o.strip() for o in settings.FRONTEND_URL.split(",") if o.strip()]
_wildcard   = not _configured or _configured == ["*"]

if _wildcard:
    _ALLOW_ORIGINS     = ["*"]
    _ALLOW_CREDENTIALS = False   # MUST be False when origins=["*"] — browser enforced
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


# ── Health / Diagnostic routes (registered FIRST — before all other routes) ──
#
# IMPORTANT: These must be registered before the API routers AND before the
# catch-all /{full_path:path} route, otherwise the catch-all intercepts them.

@app.get("/health", tags=["System"])
async def health_check():
    """Basic health check — used by Render.com uptime monitoring."""
    return {"status": "ok", "version": "4.1.1"}


@app.get("/api/ping", tags=["System"])
async def api_ping():
    """
    Diagnostic ping — no auth, no DB required.
    Returns 200 JSON if the FastAPI app is running and routes are reachable.
    The frontend calls this on login page load to detect server availability.
    """
    return {"pong": True, "version": "4.1.1", "method": "GET"}


@app.post("/api/ping", tags=["System"])
async def api_ping_post():
    """
    POST diagnostic — confirms POST requests reach FastAPI (not blocked by proxy).
    If GET /api/ping succeeds but POST /api/ping fails, the 403s on login are
    caused by a reverse-proxy or WAF blocking POST requests specifically.
    """
    return {"pong": True, "version": "4.1.1", "method": "POST"}


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


# ── Static / SPA Frontend routes ──────────────────────────────────────────────
#
# IMPORTANT: The catch-all /{full_path:path} must be registered LAST.
# It must NOT intercept /api/* paths — those are handled by the routers above.
# FastAPI matches routes in registration order; the routers above will match
# any /api/* request before this catch-all is reached.

@app.get("/google-callback.html", response_class=FileResponse, tags=["Static"])
async def serve_google_callback():
    cb = FRONTEND_DIR / "google-callback.html"
    return FileResponse(str(cb if cb.exists() else INDEX_HTML), media_type="text/html")


@app.get("/", response_class=FileResponse, tags=["Static"])
async def serve_root():
    return FileResponse(str(INDEX_HTML), media_type="text/html")


@app.get("/{full_path:path}", response_class=FileResponse, tags=["Static"])
async def serve_frontend(full_path: str):
    """
    SPA catch-all — serves index.html for any non-API frontend route.
    API routes (/api/*) are matched by the routers above and never reach here.
    """
    return FileResponse(str(INDEX_HTML), media_type="text/html")
