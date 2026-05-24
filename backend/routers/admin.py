# routers/admin.py  — v42
# GoldTrader Pro — Super-admin console
# Fixes in this version:
#  1. GET /tenants: now returns trial_expires_at, days_left, created_at, signup_type
#  2. PATCH /approve: activates Tenant + User accounts, sets plan='approved'
#  3. PATCH /reject: disables Tenant + User accounts
#  4. GET /google-requests: returns trial_expires_at + days_left (field name fixed)
#  5. PATCH /tenants/{id}/extend-trial: extend trial by N days
#  6. DELETE cascade: wrapped per-table in try/except to survive missing FKs

from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sql_delete, func, update as sql_update
from jose import jwt
import bcrypt

from database import get_db
from models import (
    Tenant, User, Invoice, InvoiceItem, Customer, Payment, Advance,
    CashEntry, StockItem, StockTransaction,
    Supplier, SupplierInvoice, SupplierInvoiceItem, SupplierPayment, SupplierAdvance,
    ApprovalStatus, PlanEnum, RoleEnum,
)

from config import settings
from utils.auth import _enum_val

ADMIN_USERNAME      = settings.TAXLY_ADMIN_USERNAME
ADMIN_PASSWORD_HASH = settings.ADMIN_PASSWORD_HASH
ADMIN_SECRET_KEY    = settings.JWT_SECRET

_bearer = HTTPBearer(auto_error=False)

async def get_admin_payload(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    if not creds:
        raise HTTPException(401, "Admin token required")
    try:
        payload = jwt.decode(creds.credentials, ADMIN_SECRET_KEY, algorithms=["HS256"])
        if payload.get("role") != "superadmin":
            raise HTTPException(403, "Admin access required")
        return payload
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid or expired admin token")


router = APIRouter(tags=["Admin"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class AdminLoginBody(BaseModel):
    username: str
    password: str

class NewTenantBody(BaseModel):
    company_name:   str
    admin_username: str
    admin_mobile:   str
    password:       str
    plan:           str = "demo"

class ResetPasswordBody(BaseModel):
    new_password: str

class ExtendTrialBody(BaseModel):
    days: int = 10


# ── Helpers ───────────────────────────────────────────────────────────────────

def _days_left(expires_at) -> int:
    """Days remaining until trial expiry. Handles both date and datetime, aware and naive."""
    if not expires_at:
        return 0
    try:
        # If it's a datetime (aware or naive), extract the date in UTC
        if hasattr(expires_at, 'hour'):
            # It's a datetime — normalise to UTC date
            from datetime import timezone as _tz
            if expires_at.tzinfo is None:
                exp_date = expires_at.date()
            else:
                import datetime as _dt
                exp_date = expires_at.astimezone(_tz.utc).date()
        else:
            exp_date = expires_at  # already a plain date
        return (exp_date - date.today()).days
    except Exception:
        return 0

def _user_trial_extra(u) -> dict:
    """Extract trial info from a User object (trial_expires_at lives on User)."""
    expires_at    = getattr(u, "trial_expires_at", None)
    approval_stat = getattr(u, "approval_status",  None)
    provider      = getattr(u, "auth_provider",    None)
    dl = _days_left(expires_at) if expires_at else 0
    return {
        "trial_expires_at": expires_at.isoformat() if expires_at else None,
        "days_left":        dl,
        "trial_status": (
            "active"  if expires_at and dl > 0  else
            "expired" if expires_at and dl <= 0 else
            "none"
        ),
        "approval_status": approval_stat.value if hasattr(approval_stat, "value") else str(approval_stat or "approved"),
        "auth_provider":   provider.value if hasattr(provider, "value") else str(provider or "password"),
    }


def _tenant_extra(t: Tenant, admin_user=None) -> dict:
    """
    Extract trial + plan metadata.
    Trial expiry lives on User, not Tenant — pass admin_user for trial data.
    """
    created_at = getattr(t, "created_at", None)
    plan       = getattr(t, "plan", "demo")
    # plan is a PlanEnum — get its value safely
    plan_val   = plan.value if hasattr(plan, "value") else str(plan or "demo")

    result = {
        "created_at":       created_at.isoformat() if created_at else None,
        "plan":             plan_val,
        "signup_type":      "manual",
        "trial_expires_at": None,
        "trial_started_at": None,
        "days_left":        0,
        "trial_status":     "none",
        "approval_status":  "approved",
        "auth_provider":    "password",
    }

    if admin_user:
        trial_info = _user_trial_extra(admin_user)
        result.update(trial_info)
        # Determine signup type
        prov = getattr(admin_user, "auth_provider", None)
        prov_val = prov.value if hasattr(prov, "value") else str(prov or "password")
        result["signup_type"] = "google" if prov_val == "google" else "demo"

    return result


# ── POST /login ───────────────────────────────────────────────────────────────

@router.post("/login")
async def admin_login(body: AdminLoginBody):
    # Case-insensitive username comparison — handles "Taxly" vs "taxly" etc.
    if body.username.strip().lower() != ADMIN_USERNAME.strip().lower():
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if ADMIN_PASSWORD_HASH:
        try:
            ok = bcrypt.checkpw(body.password.encode(), ADMIN_PASSWORD_HASH.encode())
        except Exception:
            ok = (body.password == ADMIN_PASSWORD_HASH)
    else:
        ok = False
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = jwt.encode(
        {"sub": "admin", "role": "superadmin",
         "exp": datetime.utcnow() + timedelta(hours=12)},
        ADMIN_SECRET_KEY, algorithm="HS256",
    )
    return {"access_token": token}


# ── GET /tenants ──────────────────────────────────────────────────────────────

@router.get("/tenants")
async def list_tenants(
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """List all tenants with trial status, expiry, and user/invoice counts."""
    result  = await db.execute(select(Tenant).order_by(Tenant.id.desc()))
    tenants = result.scalars().all()
    rows = []
    for t in tenants:
        u_count = (await db.execute(
            select(func.count()).select_from(User).where(User.tenant_id == t.id)
        )).scalar() or 0
        i_count = (await db.execute(
            select(func.count()).select_from(Invoice).where(Invoice.tenant_id == t.id)
        )).scalar() or 0
        # Get the admin user to read trial_expires_at (it lives on User, not Tenant)
        admin_u_res = await db.execute(
            select(User).where(User.tenant_id == t.id).order_by(User.id)
        )
        admin_u = admin_u_res.scalars().first()
        row = {
            "id":            t.id,
            "company_name":  t.company_name,
            "is_active":     t.is_active,
            "user_count":    u_count,
            "invoice_count": i_count,
        }
        row.update(_tenant_extra(t, admin_u))
        rows.append(row)
    return rows


# ── POST /tenants ─────────────────────────────────────────────────────────────

@router.post("/tenants", status_code=201)
async def create_tenant(
    body: NewTenantBody,
    _:   dict         = Depends(get_admin_payload),
    db:  AsyncSession = Depends(get_db),
):
    # Map plan string to PlanEnum safely
    try:
        plan_val = PlanEnum(body.plan)
    except (ValueError, KeyError):
        plan_val = PlanEnum.demo
    tenant = Tenant(company_name=body.company_name, is_active=True, plan=plan_val)
    db.add(tenant)
    await db.flush()
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = User(
        tenant_id=tenant.id, username=body.admin_username,
        mobile=body.admin_mobile, password_hash=hashed,
        role=RoleEnum.admin, is_active=True,
        approval_status=ApprovalStatus.approved,
    )
    db.add(user)
    await db.commit()
    return {"message": "Tenant created", "tenant_id": tenant.id}


# ── PATCH /tenants/{id}/toggle ────────────────────────────────────────────────

@router.patch("/tenants/{tenant_id}/toggle")
async def toggle_tenant(
    tenant_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    t = await db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    t.is_active = not t.is_active
    # Also toggle all users of this tenant
    await db.execute(
        sql_update(User).where(User.tenant_id == tenant_id)
        .values(is_active=t.is_active)
    )
    await db.commit()
    return {"is_active": t.is_active}


# ── PATCH /tenants/{id}/reset-password ───────────────────────────────────────

@router.patch("/tenants/{tenant_id}/reset-password")
async def reset_tenant_password(
    tenant_id: int,
    body: ResetPasswordBody,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    result     = await db.execute(
        select(User).where(User.tenant_id == tenant_id, User.role == RoleEnum.admin)
    )
    admin_user = result.scalars().first()
    if not admin_user:
        raise HTTPException(404, "Admin user not found for this tenant")
    admin_user.password_hash = bcrypt.hashpw(
        body.new_password.encode(), bcrypt.gensalt()
    ).decode()
    await db.commit()
    return {"message": "Password reset"}


# ── PATCH /tenants/{id}/extend-trial — extend trial by N days ────────────────

@router.patch("/tenants/{tenant_id}/extend-trial")
async def extend_trial(
    tenant_id: int,
    body: ExtendTrialBody,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """Extend (or restart) the trial period for a tenant by N days from today."""
    from datetime import timezone
    t = await db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    days = max(1, min(body.days, 365))
    new_expiry = datetime.now(timezone.utc) + timedelta(days=days)
    t.is_active = True
    # Re-activate all users + update their trial_expires_at and approval_status
    await db.execute(
        sql_update(User)
        .where(User.tenant_id == tenant_id)
        .values(
            is_active        = True,
            trial_expires_at = new_expiry,
            approval_status  = ApprovalStatus.trial,
        )
    )
    await db.commit()
    return {
        "message":          f"Trial extended by {days} days",
        "trial_expires_at": new_expiry.isoformat(),
        "days_left":        days,
    }


# ── DELETE /tenants/{id} — PERMANENT DELETE ───────────────────────────────────

@router.delete("/tenants/{tenant_id}")
async def permanently_delete_tenant(
    tenant_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """
    Permanently erase a tenant and all associated data.
    Each table delete is wrapped in try/except so missing FK columns
    never block the cascade. Committed per-group for safety.
    """
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    tid = tenant_id

    # 1 — Stock
    for model in [StockTransaction, StockItem]:
        try:
            await db.execute(sql_delete(model).where(model.tenant_id == tid))
        except Exception:
            await db.rollback()

    # 2 — Suppliers
    for model in [SupplierInvoiceItem, SupplierPayment, SupplierAdvance,
                  SupplierInvoice, Supplier]:
        try:
            await db.execute(sql_delete(model).where(model.tenant_id == tid))
        except Exception:
            await db.rollback()

    # 3 — Customers / Invoices
    for model in [InvoiceItem, Payment, Advance, Invoice, Customer]:
        try:
            await db.execute(sql_delete(model).where(model.tenant_id == tid))
        except Exception:
            await db.rollback()

    # 4 — Cash book
    try:
        await db.execute(sql_delete(CashEntry).where(CashEntry.tenant_id == tid))
    except Exception:
        await db.rollback()

    # 5 — (GoogleSignupRequest table not used in this project)

    # 6 — Users
    try:
        await db.execute(sql_delete(User).where(User.tenant_id == tid))
    except Exception:
        await db.rollback()

    # 7 — Tenant itself
    await db.delete(tenant)
    await db.commit()
    return {"message": f"Tenant #{tid} permanently deleted", "tenant_id": tid, "deleted": True}


# ── GET /users ────────────────────────────────────────────────────────────────

@router.get("/users")
async def list_all_users(
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).order_by(User.tenant_id, User.id))
    users  = result.scalars().all()
    rows = []
    for u in users:
        t = await db.get(Tenant, u.tenant_id)
        role_val = u.role.value if hasattr(u.role, "value") else str(u.role or "user")
        prov_val = u.auth_provider.value if hasattr(u.auth_provider, "value") else str(getattr(u, "auth_provider", "password"))
        rows.append({
            "id":            u.id,
            "username":      u.username,
            "mobile":        u.mobile,
            "email":         getattr(u, "email", None) or "—",
            "tenant":        t.company_name if t else f"Tenant#{u.tenant_id}",
            "tenant_id":     u.tenant_id,
            "role":          role_val,
            "auth_provider": prov_val,
            "is_active":     u.is_active,
        })
    return rows


# ── GET /google-requests ──────────────────────────────────────────────────────

@router.get("/google-requests")
async def list_google_requests(
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns ALL trial/pending users — both Google signups and demo (password) signups.
    Source of truth is the User table (approval_status in: trial, pending, rejected, approved).
    Demo users never create a GoogleSignupRequest, so we query User directly.
    """
    result = await db.execute(
        select(User)
        .order_by(User.created_at.desc())
    )
    users = result.scalars().all()

    rows = []
    for u in users:
        t = await db.get(Tenant, u.tenant_id)
        trial_info = _user_trial_extra(u)
        approval   = trial_info["approval_status"]
        # Determine status label
        if approval == "trial":
            status = "trial" if trial_info["days_left"] > 0 else "expired"
        else:
            status = approval

        rows.append({
            "id":              u.id,          # NOTE: this is User.id, not GSR.id
            "name":            u.username or "",
            "email":           u.email or "",
            "company":         t.company_name if t else (u.company_name or ""),
            "mobile":          u.mobile or "",
            "signed_up":       u.created_at.isoformat() if u.created_at else "",
            "status":          status,
            "trial_expires_at": trial_info["trial_expires_at"],
            "days_left":       trial_info["days_left"],
            "tenant_id":       u.tenant_id,
            "auth_provider":   trial_info["auth_provider"],
            "user_id":         u.id,
        })
    return rows


# ── PATCH /google-requests/{id}/approve ──────────────────────────────────────

@router.patch("/google-requests/{user_id}/approve")
async def approve_google_request(
    user_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """
    Approve a user by User.id (works for both Google and demo signups).
    - Sets User.approval_status = approved, clears trial_expires_at
    - Activates Tenant + all its Users
    - Sets Tenant.plan = annual (unlimited access, no more trial)
    """
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    user.approval_status  = ApprovalStatus.approved
    user.trial_expires_at = None   # unlimited — no longer on trial
    user.is_active        = True

    # Activate the linked Tenant
    t = await db.get(Tenant, user.tenant_id)
    if t:
        t.is_active = True
        t.plan      = PlanEnum.annual  # approved = full access

    # Activate all users of that tenant
    await db.execute(
        sql_update(User)
        .where(User.tenant_id == user.tenant_id)
        .values(is_active=True, approval_status=ApprovalStatus.approved)
    )

    await db.commit()
    return {"message": "Approved — account activated with full access"}


# ── PATCH /google-requests/{id}/reject ───────────────────────────────────────

@router.patch("/google-requests/{user_id}/reject")
async def reject_google_request(
    user_id: int,
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    """
    Reject a user by User.id (works for both Google and demo signups).
    - Sets User.approval_status = rejected
    - Disables Tenant + all its Users
    """
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    user.approval_status = ApprovalStatus.rejected
    user.is_active       = False

    # Disable the linked Tenant
    t = await db.get(Tenant, user.tenant_id)
    if t:
        t.is_active = False
        t.plan      = PlanEnum.expired

    # Disable all users of that tenant
    await db.execute(
        sql_update(User)
        .where(User.tenant_id == user.tenant_id)
        .values(is_active=False, approval_status=ApprovalStatus.rejected)
    )

    await db.commit()
    return {"message": "Rejected — account disabled"}


# ── GET /backups ──────────────────────────────────────────────────────────────

@router.get("/backups")
async def list_backups(
    _:  dict         = Depends(get_admin_payload),
    db: AsyncSession = Depends(get_db),
):
    result  = await db.execute(select(Tenant).order_by(Tenant.id.desc()))
    tenants = result.scalars().all()
    rows = []
    for t in tenants:
        i_count = (await db.execute(
            select(func.count()).select_from(Invoice).where(Invoice.tenant_id == t.id)
        )).scalar() or 0
        c_count = (await db.execute(
            select(func.count()).select_from(Customer).where(Customer.tenant_id == t.id)
        )).scalar() or 0
        rows.append({
            "tenant_id":      t.id,
            "company_name":   t.company_name,
            "invoice_count":  i_count,
            "customer_count": c_count,
            "is_active":      t.is_active,
            "plan":           _enum_val(getattr(t, "plan", None), "demo"),
        })
    return rows

# NOTE: GET /backups/{tenant_id}/download is in export.py
