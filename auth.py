import base64
import hashlib
import hmac
import json
import os
import time
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import Role, User

SECRET_KEY = os.getenv("TOC_JWT_SECRET", "dev-local-toc-advisory-secret-change-in-prod")
TOKEN_TTL_SECONDS = 12 * 3600

bearer_scheme = HTTPBearer(auto_error=False)

ROLE_DEFINITIONS = {
    "responder": "Field Responder — logs road incidents, retrieves telemetry, and views incident status.",
    "dispatcher": "TOC Dispatcher — runs severity assessments, assigns units, and updates clearance status.",
    "supervisor": "TOC Supervisor — oversees operations analytics, manages user roles, and audits/deletes records.",
}


def hash_password(password: str) -> str:
    """Hash a password using salted scrypt."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored salted scrypt hash."""
    try:
        salt_hex, digest_hex = stored_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return hmac.compare_digest(candidate, expected)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def create_access_token(user_id: int, role: str, ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    """Create an HS256-signed bearer token."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    now = int(time.time())
    payload = _b64url_encode(
        json.dumps({"sub": str(user_id), "role": role, "iat": now, "exp": now + ttl_seconds}, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(SECRET_KEY.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url_encode(sig)}"


def decode_access_token(token: str) -> dict:
    """Validate and decode an HS256 bearer token."""
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed authentication token.")
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        provided_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(provided_sig, expected_sig):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token signature.")
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token.")

    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication token has expired.")
    return payload


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve and validate the authenticated active user."""
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject.")

    user = db.query(User).options(joinedload(User.role)).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found or deactivated.")
    return user


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User | None:
    """Return the authenticated user if a valid bearer token is provided, otherwise None."""
    if not credentials:
        return None
    return get_current_user(credentials, db)


def require_roles(*allowed_roles: str):
    """Dependency enforcing that the current user holds one of the allowed roles."""
    def _checker(user: User = Depends(get_current_user)) -> User:
        role_name = user.role.name if user.role else ""
        if role_name not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Action restricted to roles: {', '.join(allowed_roles)}.",
            )
        return user

    return _checker


def seed_initial_data(db: Session) -> None:
    """Seed normalized roles and initial demo accounts if absent."""
    roles_by_name = {}
    for role_name, desc in ROLE_DEFINITIONS.items():
        role = db.query(Role).filter(Role.name == role_name).first()
        if not role:
            role = Role(name=role_name, description=desc)
            db.add(role)
            db.flush()
        roles_by_name[role_name] = role

    demo_accounts = [
        ("Alex Rivera (Field Responder)", "responder@toc.gov", "TocDemo@2026", "responder"),
        ("Maya Chen (TOC Dispatcher)", "dispatcher@toc.gov", "TocDemo@2026", "dispatcher"),
        ("Jordan Patel (TOC Supervisor)", "supervisor@toc.gov", "TocDemo@2026", "supervisor"),
    ]
    for full_name, email, raw_pw, role_name in demo_accounts:
        existing = db.query(User).filter(User.email == email).first()
        if not existing:
            db.add(
                User(
                    full_name=full_name,
                    email=email,
                    password_hash=hash_password(raw_pw),
                    role_id=roles_by_name[role_name].id,
                    is_active=True,
                )
            )
    db.commit()
