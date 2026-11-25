"""Session authentication: PBKDF2 password, opaque database sessions, CSRF and origin checks."""
import hashlib
import hmac
import os
import secrets
import time
from datetime import timedelta
from fastapi import HTTPException, Request, Response
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from .models import AdminSession, utcnow
from . import admin_settings

COOKIE_NAME = "webapp_session"
TTL_HOURS = 12
_failures = []

def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must have at least 12 characters")
    salt = secrets.token_bytes(16)
    rounds = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"

def check_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt, expected = encoded.split("$")
        if scheme != "pbkdf2_sha256" or not (310_000 <= int(rounds) <= 1_000_000):
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False

def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()

def session_csrf(raw_session: str) -> str:
    """Derive a stable token from the secret cookie using a purpose-specific label."""
    return hmac.new(raw_session.encode(), b"webapp/csrf/v1", hashlib.sha256).hexdigest()


def require_origin(request: Request):
    origin = request.headers.get("origin")
    configured = os.getenv("APP_PUBLIC_ORIGIN", "").rstrip("/")
    if origin:
        allowed = {configured} if configured else set()
        if os.getenv("APP_ALLOW_LOCAL_DEV") == "true":
            allowed.update({"http://127.0.0.1:5178", "http://localhost:5178", "http://127.0.0.1:18080", "http://localhost:18080"})
        if origin not in allowed:
            raise HTTPException(403, "Origin not permitted")

def login(request: Request, response: Response, db: Session, username: str, password: str):
    require_origin(request)
    now = time.monotonic()
    _failures[:] = [t for t in _failures if now - t < 900]
    if len(_failures) >= 10:
        raise HTTPException(429, "Too many unsuccessful login attempts; retry later")
    expected_user = os.getenv("APP_ADMIN_USERNAME", "admin")
    encoded = admin_settings.read()["password_hash"] or os.getenv("APP_ADMIN_PASSWORD_HASH", "")
    if not encoded:
        raise HTTPException(503, "Admin password hash not configured")
    user_ok = hmac.compare_digest(username.encode(), expected_user.encode())
    password_ok = check_password(password, encoded)
    if not (user_ok and password_ok):
        _failures.append(now)
        raise HTTPException(401, "Incorrect credentials")
    raw_token = secrets.token_urlsafe(48)
    raw_csrf = session_csrf(raw_token)
    row = AdminSession(token_hash=token_hash(raw_token), csrf_hash=token_hash(raw_csrf), username=expected_user,
                       expires_at=utcnow() + timedelta(hours=TTL_HOURS))
    db.add(row)
    db.commit()
    response.set_cookie(key=COOKIE_NAME, value=raw_token, secure=os.getenv("APP_COOKIE_SECURE", "true").lower()=="true",
                        httponly=True, samesite="strict", max_age=TTL_HOURS*3600, path="/api")
    return {"authenticated": True, "username": expected_user, "csrf_token": raw_csrf, "expires_in": TTL_HOURS*3600}

def session_row(request: Request, db: Session) -> AdminSession | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    row = db.scalar(select(AdminSession).where(AdminSession.token_hash == token_hash(raw)))
    if not row:
        return None
    now = utcnow()
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=now.tzinfo)
    if expires <= now:
        return None
    return row

def session_info(request: Request, db: Session):
    row = session_row(request, db)
    if row is None:
        raise HTTPException(401, "No active session")
    csrf = session_csrf(request.cookies[COOKIE_NAME])
    return {"authenticated": True, "username": row.username, "csrf_token": csrf}

def authorize_request(request: Request, db: Session):
    bearer = request.headers.get("authorization", "")
    secret = os.getenv("APP_API_TOKEN", "")
    if secret and hmac.compare_digest(bearer, "Bearer " + secret):
        return "service"
    return authorize_session(request, db)


def authorize_session(request: Request, db: Session):
    row = session_row(request, db)
    if row is None:
        raise HTTPException(401, "Login required")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        require_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        csrf_digest = token_hash(csrf)
        current_digest = token_hash(session_csrf(request.cookies[COOKIE_NAME]))
        # Existing sessions can still use their last pre-upgrade token until expiry.
        if not (hmac.compare_digest(csrf_digest, current_digest)
                or hmac.compare_digest(csrf_digest, row.csrf_hash)):
            raise HTTPException(403, "CSRF token invalid")
    return row.username

def logout(request: Request, response: Response, db: Session):
    authorize_request(request, db)
    row = session_row(request, db)
    if row is not None:
        db.delete(row)
        db.commit()
    response.delete_cookie(COOKIE_NAME, path="/api", secure=os.getenv("APP_COOKIE_SECURE", "true").lower()=="true", httponly=True, samesite="strict")
    return {"ok": True}


def change_password(request: Request, db: Session, old_password: str, new_password: str):
    """Change shared admin credential and revoke all sessions after persisting.

    The initial environment password remains bootstrap-only once overridden.
    """
    authorize_session(request, db)
    if len(new_password) < 12 or len(new_password) > 256:
        raise HTTPException(422, "新密码长度须为 12–256 个字符")
    current = admin_settings.read()
    encoded = current["password_hash"] or os.getenv("APP_ADMIN_PASSWORD_HASH", "")
    if not encoded or not check_password(old_password, encoded):
        raise HTTPException(403, "当前密码错误")
    if hmac.compare_digest(old_password, new_password):
        raise HTTPException(422, "新密码不能与当前密码相同")
    current["password_hash"] = hash_password(new_password)
    try:
        admin_settings.save(current)
    except (OSError, ValueError):
        raise HTTPException(503, "密码保存失败，旧密码保持有效") from None
    db.execute(delete(AdminSession))
    db.commit()
    return {"changed": True, "sessions_revoked": True}
