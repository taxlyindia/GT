# utils/auth.py — JWT tokens, password hashing, Google OAuth verification

from datetime import datetime, timedelta, timezone
from typing import Optional
import bcrypt
from jose import jwt, JWTError
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import settings

bearer_scheme = HTTPBearer(auto_error=False)


# ── Safe enum value helpers ───────────────────────────────────
def _enum_val(v, default: str = "") -> str:
    """Return .value if v is an enum, otherwise str(v). Never crashes."""
    if v is None:
        return default
    if hasattr(v, "value"):
        return v.value
    return str(v)


def _enum_eq(v, enum_member) -> bool:
    """Compare a DB value (may be string OR enum) with an enum member safely."""
    if v is None:
        return False
    if hasattr(v, "value"):          # v is already a Python enum
        return v == enum_member
    return str(v) == enum_member.value  # v is a raw string from DB


# ── Password ─────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Hash a plain-text password using bcrypt."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── JWT ──────────────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a signed JWT access token."""
    payload = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Current user dependency ───────────────────────────────────

def get_current_user_payload(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """FastAPI dependency — validates Bearer token, returns decoded payload."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return decode_token(credentials.credentials)


def get_tenant_payload(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """
    FastAPI dependency for all tenant-scoped routes.
    Validates Bearer token AND enforces that tenant_id is present in payload.
    Use this instead of get_current_user_payload in all business routers.
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(credentials.credentials)
    if "tenant_id" not in payload or payload["tenant_id"] is None:
        raise HTTPException(
            status_code=401,
            detail="Token is not scoped to a tenant. Please log in as a regular user.",
        )
    return payload


def require_admin(payload: dict = Depends(get_tenant_payload)) -> dict:
    """FastAPI dependency — ensures the authenticated user has admin role."""
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return payload


def require_taxly_admin(payload: dict = Depends(get_current_user_payload)) -> dict:
    """FastAPI dependency — ensures the user is the Taxly super-admin."""
    if not payload.get("is_taxly_admin"):
        raise HTTPException(status_code=403, detail="Taxly admin access required")
    return payload


# ── Google OAuth ─────────────────────────────────────────────

def verify_google_token(id_token_str: str) -> dict:
    """
    Verify a Google ID token and return the decoded claims.
    Claims include: sub (google_id), email, name, picture, email_verified

    Demo mode: if GOOGLE_CLIENT_ID is not configured, the frontend sends
    a real Google credential obtained via Google Identity Services (GIS) popup.
    When GOOGLE_CLIENT_ID IS configured, full verification is performed.
    """
    # ── Real Google token verification ───────────────────────
    if settings.GOOGLE_CLIENT_ID:
        try:
            claims = id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID,
            )
            if not claims.get("email_verified"):
                raise HTTPException(status_code=400, detail="Google email not verified")
            return claims
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid Google token: {e}")

    # ── Demo / development mode: decode without verification ─
    # The frontend uses Google One Tap / GIS which returns a real JWT.
    # Without a CLIENT_ID configured we decode it unverified (for demo only).
    import base64, json as _json
    try:
        parts = id_token_str.split(".")
        if len(parts) != 3:
            raise ValueError("Not a JWT")
        # Decode payload (add padding as needed)
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        email = payload.get("email", "")
        if not email:
            raise ValueError("No email in token")
        return {
            "sub":            payload.get("sub", email),
            "email":          email,
            "name":           payload.get("name", email.split("@")[0]),
            "picture":        payload.get("picture", ""),
            "email_verified": payload.get("email_verified", True),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid Google token: {e}")


# ── Trial helpers ─────────────────────────────────────────────

def _ensure_aware(dt: datetime) -> datetime:
    """Make a datetime timezone-aware (UTC) if it isn't already."""
    if dt is None:
        return dt
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_trial_active(trial_expires_at: Optional[datetime]) -> bool:
    """Return True if the trial period has not yet expired (timezone-safe)."""
    if not trial_expires_at:
        return False
    try:
        return datetime.now(timezone.utc) < _ensure_aware(trial_expires_at)
    except Exception:
        return False


def trial_days_remaining(trial_expires_at: Optional[datetime]) -> int:
    """Return the number of full days remaining in the trial (0 if expired)."""
    if not trial_expires_at:
        return 0
    try:
        delta = _ensure_aware(trial_expires_at) - datetime.now(timezone.utc)
        return max(0, delta.days)
    except Exception:
        return 0
