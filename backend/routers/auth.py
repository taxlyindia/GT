# routers/auth.py — Authentication endpoints

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import User, Tenant, ApprovalStatus, AuthProvider, RoleEnum, PlanEnum
from utils.auth import (
    verify_password, hash_password, create_access_token,
    verify_google_token, get_current_user_payload,
    is_trial_active, trial_days_remaining,
    _enum_val, _enum_eq
)
from config import settings

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────

class PasswordLoginRequest(BaseModel):
    username_or_mobile: str
    password: str

class GoogleLoginRequest(BaseModel):
    id_token: str                       # Google ID token from frontend

class GoogleSignupRequest(BaseModel):
    id_token: str
    company_name: str = Field(..., min_length=2, max_length=200)
    mobile: str       = Field(..., pattern=r"^\d{10}$")

class DemoSignupRequest(BaseModel):
    name:         str = Field(..., min_length=2, max_length=100)
    mobile:       str = Field(..., pattern=r"^\d{10}$")
    company_name: str = Field(..., min_length=2, max_length=200)
    password:     str = Field(..., min_length=8)

class TokenResponse(BaseModel):
    access_token:    str
    token_type:      str = "bearer"
    user_name:       str
    user_role:       str
    tenant_name:     str
    trial_active:    bool = False
    trial_days_left: int  = 0


# ── Password Login ────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def password_login(body: PasswordLoginRequest, db: AsyncSession = Depends(get_db)):
    """Login with username/mobile + password."""
    result = await db.execute(
        select(User).where(
            (User.username == body.username_or_mobile) |
            (User.mobile   == body.username_or_mobile)
        )
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Safe bcrypt check — Google-signup users have a placeholder hash;
    # catching ValueError prevents a 500 when the hash is invalid.
    try:
        pwd_ok = verify_password(body.password, user.password_hash)
    except Exception:
        pwd_ok = False
    if not pwd_ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled. Contact admin.")

    # ── Trial / approval check — use _enum_eq to handle string OR enum values ──
    approval = _enum_val(user.approval_status, "approved")

    if approval == "trial":
        if not is_trial_active(user.trial_expires_at):
            # Trial expired — move to pending
            user.approval_status = ApprovalStatus.pending
            await db.commit()
            raise HTTPException(
                status_code=403,
                detail="Your 10-day trial has ended. Awaiting admin approval.",
            )
    elif approval == "pending":
        raise HTTPException(
            status_code=403,
            detail="Your account is pending admin approval.",
        )
    elif approval == "rejected":
        raise HTTPException(status_code=403, detail="Account access denied.")

    tenant    = await db.get(Tenant, user.tenant_id)
    role_str  = _enum_val(user.role, "user")
    days_left = trial_days_remaining(user.trial_expires_at) if approval == "trial" else 0

    token = create_access_token({
        "sub":          str(user.id),
        "tenant_id":    user.tenant_id,
        "role":         role_str,
        "mobile":       user.mobile,
        "trial_active": approval == "trial",
    })

    return TokenResponse(
        access_token=token,
        user_name=user.username or body.username_or_mobile,
        user_role=role_str,
        tenant_name=tenant.company_name if tenant else "",
        trial_active=approval == "trial",
        trial_days_left=days_left,
    )


# ── Google Login (existing approved account) ──────────────────

@router.post("/google/login", response_model=TokenResponse)
async def google_login(body: GoogleLoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Login with Google ID token.
    - If user exists and approved → login
    - If user is on active trial → login with trial info
    - If trial expired → reject, prompt for admin approval
    - If no account → redirect to signup
    """
    claims    = verify_google_token(body.id_token)
    google_id = claims["sub"]
    email     = claims["email"]

    result = await db.execute(select(User).where(User.google_id == google_id))
    user   = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="No account found. Please sign up with Google first.",
            headers={"X-Action": "signup"},
        )

    # Check trial / approval status — safe for both string and enum DB values
    approval = _enum_val(user.approval_status, "approved")

    if approval == "trial":
        if not is_trial_active(user.trial_expires_at):
            user.approval_status = ApprovalStatus.pending
            await db.commit()
            raise HTTPException(
                status_code=403,
                detail="Your 10-day trial has ended. Awaiting admin approval.",
                headers={"X-Action": "pending"},
            )
    elif approval == "pending":
        raise HTTPException(
            status_code=403,
            detail="Your account is pending admin approval.",
            headers={"X-Action": "pending"},
        )
    elif approval == "rejected":
        raise HTTPException(status_code=403, detail="Account access denied.")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled.")

    tenant    = await db.get(Tenant, user.tenant_id)
    approval  = _enum_val(user.approval_status, "approved")
    role_str  = _enum_val(user.role, "user")
    days_left = trial_days_remaining(user.trial_expires_at) if approval == "trial" else 0

    token = create_access_token({
        "sub":          str(user.id),
        "tenant_id":    user.tenant_id,
        "role":         role_str,
        "mobile":       user.mobile,
        "trial_active": approval == "trial",
    })

    return TokenResponse(
        access_token=token,
        user_name=user.username or email,
        user_role=role_str,
        tenant_name=tenant.company_name if tenant else "",
        trial_active=approval == "trial",
        trial_days_left=days_left,
    )


# ── Google Signup (new account — 10-day trial) ───────────────

@router.post("/google/signup", response_model=TokenResponse, status_code=201)
async def google_signup(body: GoogleSignupRequest, db: AsyncSession = Depends(get_db)):
    """
    Register a new user via Google OAuth.
    - Creates a Tenant + admin User immediately
    - Sets trial_expires_at = now + 10 days
    - No admin approval needed for trial period
    - After 10 days: status → pending, admin must approve to continue
    """
    claims    = verify_google_token(body.id_token)
    google_id = claims["sub"]
    email     = claims["email"]
    name      = claims.get("name", email)

    # Check if Google account already has an account
    result = await db.execute(select(User).where(User.google_id == google_id))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Google account already registered. Please login.")

    # Create Tenant
    tenant = Tenant(
        company_name=body.company_name,
        plan=PlanEnum.demo,
        is_active=True,
    )
    db.add(tenant)
    await db.flush()   # get tenant.id

    # Create admin user with 10-day trial
    trial_expires = datetime.now(timezone.utc) + timedelta(days=settings.TRIAL_DAYS)
    user = User(
        tenant_id=tenant.id,
        username=name,
        mobile=body.mobile,
        email=email,
        password_hash=hash_password("google-oauth-no-password"),  # placeholder
        role=RoleEnum.admin,
        auth_provider=AuthProvider.google,
        google_id=google_id,
        approval_status=ApprovalStatus.trial,
        trial_expires_at=trial_expires,
        company_name=body.company_name,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({
        "sub":          str(user.id),
        "tenant_id":    tenant.id,
        "role":         RoleEnum.admin.value,
        "mobile":       body.mobile,
        "trial_active": True,
    })

    return TokenResponse(
        access_token=token,
        user_name=name,
        user_role=RoleEnum.admin.value,
        tenant_name=body.company_name,
        trial_active=True,
        trial_days_left=settings.TRIAL_DAYS,
    )


# ── Demo Signup (password-based, no Google) ───────────────────

@router.post("/signup-demo", response_model=TokenResponse, status_code=201)
async def demo_signup(body: DemoSignupRequest, db: AsyncSession = Depends(get_db)):
    """
    Register with name + mobile + company + password.
    10-day trial, no approval needed.
    """
    # Check mobile uniqueness globally
    result = await db.execute(select(User).where(User.mobile == body.mobile))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Mobile number already registered.")

    trial_expires = datetime.now(timezone.utc) + timedelta(days=settings.TRIAL_DAYS)

    tenant = Tenant(company_name=body.company_name, plan=PlanEnum.demo, is_active=True)
    db.add(tenant)
    await db.flush()

    user = User(
        tenant_id=tenant.id,
        username=body.name,
        mobile=body.mobile,
        password_hash=hash_password(body.password),
        role=RoleEnum.admin,
        auth_provider=AuthProvider.password,
        approval_status=ApprovalStatus.trial,
        trial_expires_at=trial_expires,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({
        "sub":          str(user.id),
        "tenant_id":    tenant.id,
        "role":         RoleEnum.admin.value,
        "mobile":       body.mobile,
        "trial_active": True,
    })

    return TokenResponse(
        access_token=token,
        user_name=body.name,
        user_role=RoleEnum.admin.value,
        tenant_name=body.company_name,
        trial_active=True,
        trial_days_left=settings.TRIAL_DAYS,
    )


# ── Trial Status Check ────────────────────────────────────────

@router.get("/trial-status")
async def trial_status(
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    """Check if the current user's trial is still active."""
    user = await db.get(User, int(payload["sub"]))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    active    = is_trial_active(user.trial_expires_at)
    days_left = trial_days_remaining(user.trial_expires_at)

    if not active and user.approval_status == ApprovalStatus.trial:
        user.approval_status = ApprovalStatus.pending
        await db.commit()

    return {
        "trial_active":    active,
        "trial_days_left": days_left,
        "approval_status": user.approval_status.value,
        "trial_expires_at": user.trial_expires_at,
    }



# ── Google OAuth2 Callback (code exchange) ───────────────────
import httpx as _httpx

class GoogleCallbackRequest(BaseModel):
    code: str
    redirect_uri: str

@router.post("/google/callback")
async def google_oauth_callback(body: GoogleCallbackRequest, db: AsyncSession = Depends(get_db)):
    """
    Exchange an OAuth2 authorization code for tokens.
    Called by the frontend after Google redirects back with a code.
    Returns the id_token so the existing google/login and google/signup flows work.
    """
    try:
        async with _httpx.AsyncClient() as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code":          body.code,
                    "client_id":     settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri":  body.redirect_uri,
                    "grant_type":    "authorization_code",
                },
            )
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Google token exchange failed: {resp.text}")
        token_data = resp.json()
        id_token_str = token_data.get("id_token", "")
        if not id_token_str:
            raise HTTPException(status_code=400, detail="No id_token returned from Google")
        # Decode claims for display (already verified by Google)
        import base64 as _b64, json as _json
        parts = id_token_str.split(".")
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        claims = _json.loads(_b64.urlsafe_b64decode(padded))
        return {
            "id_token": id_token_str,
            "email":    claims.get("email", ""),
            "name":     claims.get("name",  claims.get("email","").split("@")[0]),
            "picture":  claims.get("picture", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Google auth error: {str(e)}")

# ── Company Profile ────────────────────────────────────────────────────────
# GET  /api/auth/profile  — load current tenant profile
# PUT  /api/auth/profile  — save (update) tenant profile
# Fix: "Method Not Allowed" was caused by missing GET + PUT; only POST existed.

from typing import Optional as _Opt

class CompanyProfileUpdate(BaseModel):
    company_name:       str            = Field(..., min_length=2, max_length=200)
    gstin:              _Opt[str]      = None
    phone:              _Opt[str]      = None
    email:              _Opt[str]      = None
    address:            _Opt[str]      = None
    state:              _Opt[str]      = None
    pan:                _Opt[str]      = None
    upi_id:             _Opt[str]      = None
    qr_code_url:        _Opt[str]      = None
    bank_name:          _Opt[str]      = None
    bank_account_no:    _Opt[str]      = None
    bank_ifsc:          _Opt[str]      = None
    bank_branch:        _Opt[str]      = None
    terms_conditions:   _Opt[str]      = None
    logo_url:           _Opt[str]      = None
    authorised_person:  _Opt[str]      = None


@router.get("/profile")
async def get_company_profile(
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """Return current tenant's company profile for the Settings page."""
    tenant = await db.get(Tenant, payload["tenant_id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {
        "company_name":      tenant.company_name,
        "gstin":             tenant.gstin,
        "phone":             tenant.phone,
        "email":             tenant.email,
        "address":           tenant.address,
        "state":             tenant.state,
        "logo_url":          tenant.logo_url,
        "pan":               tenant.pan,
        "upi_id":            tenant.upi_id,
        "qr_code_url":       tenant.qr_code_url,
        "bank_name":         tenant.bank_name,
        "bank_account_no":   tenant.bank_account_no,
        "bank_ifsc":         tenant.bank_ifsc,
        "bank_branch":       tenant.bank_branch,
        "terms_conditions":  tenant.terms_conditions,
        "authorised_person": tenant.authorised_person,
    }


@router.put("/profile")
async def update_company_profile(
    body:    CompanyProfileUpdate,
    payload: dict         = Depends(get_current_user_payload),
    db:      AsyncSession = Depends(get_db),
):
    """
    Save company profile (name, GSTIN, address, logo, bank details, UPI, terms).
    Printed on every invoice PDF.
    Fix: previously returned 405 Method Not Allowed because only POST /signup
    was registered on /api/auth — GET and PUT were missing.
    """
    tenant = await db.get(Tenant, payload["tenant_id"])
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant.company_name       = body.company_name
    if body.gstin              is not None: tenant.gstin              = body.gstin
    if body.phone              is not None: tenant.phone              = body.phone
    if body.email              is not None: tenant.email              = body.email
    if body.address            is not None: tenant.address            = body.address
    if body.state              is not None: tenant.state              = body.state
    if body.logo_url           is not None: tenant.logo_url           = body.logo_url
    if body.pan                is not None: tenant.pan                = body.pan
    if body.upi_id             is not None: tenant.upi_id             = body.upi_id
    if body.qr_code_url        is not None: tenant.qr_code_url        = body.qr_code_url
    if body.bank_name          is not None: tenant.bank_name          = body.bank_name
    if body.bank_account_no    is not None: tenant.bank_account_no    = body.bank_account_no
    if body.bank_ifsc          is not None: tenant.bank_ifsc          = body.bank_ifsc
    if body.bank_branch        is not None: tenant.bank_branch        = body.bank_branch
    if body.terms_conditions   is not None: tenant.terms_conditions   = body.terms_conditions
    if body.authorised_person  is not None: tenant.authorised_person  = body.authorised_person

    await db.commit()
    await db.refresh(tenant)
    return {"message": "Company profile saved", "company_name": tenant.company_name}
