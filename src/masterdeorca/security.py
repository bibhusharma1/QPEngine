"""Bearer token authentication for the initial single-admin API."""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.settings import JWT_SECRET, JWT_TTL_MINUTES
from src.masterdeorca import database

bearer = HTTPBearer(auto_error=False)


def issue_token(email: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": email, "iat": now, "exp": now + timedelta(minutes=JWT_TTL_MINUTES)}, JWT_SECRET, algorithm="HS256")


def require_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        subject = payload.get("sub")
        if not subject:
            raise ValueError("token has no subject")
        email = str(subject).lower()
        with database.connect() as db:
            row = db.execute("SELECT status FROM users WHERE email=?", (email,)).fetchone()
        if not row or row["status"] not in {"active", "approved"}:
            raise HTTPException(status_code=401, detail="User account is not active", headers={"WWW-Authenticate": "Bearer"})
        return email
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from exc


def require_admin(email: str = Depends(require_user)) -> str:
    with database.connect() as db:
        row = db.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
    if not row or row["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return email


def _administrator_mode(email: str) -> str:
    with database.connect() as db:
        row = db.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
        role = row["role"] if row else "teacher"
    if role != "admin":
        return "edit"  # Non-admin principals keep their established permissions.
    return database.get_admin_access_mode(email)


READONLY_DETAIL = "Administrator account is in read-only mode; editing is disabled"


def require_write_user(email: str = Depends(require_user)) -> str:
    """Gate record-mutating routes; administrators must not be in readonly mode."""
    if _administrator_mode(email) == "readonly":
        raise HTTPException(status_code=403, detail=READONLY_DETAIL)
    return email


def require_admin_write(email: str = Depends(require_admin)) -> str:
    """Gate administrative mutations; read-only administrators receive 403."""
    if database.get_admin_access_mode(email) == "readonly":
        raise HTTPException(status_code=403, detail=READONLY_DETAIL)
    return email


def _user_role(email: str) -> str:
    with database.connect() as db:
        row = db.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
    return row["role"] if row else "teacher"


def accessible_subjects(email: str) -> set[str] | None:
    """Subject names the principal may access; None means unrestricted (admin).

    Read from the database on every call so assignment changes take effect
    immediately, without re-login or token claims.
    """
    if _user_role(email) != "teacher":
        return None
    return set(database.get_teacher_subjects(email))


def subject_allowed(email: str, subject_id: str | None) -> bool:
    """Read access: admins are unrestricted; teachers need the assignment.

    Generic resources (subject_id None) stay readable by every principal.
    """
    allowed = accessible_subjects(email)
    return allowed is None or subject_id is None or subject_id in allowed


def require_subject_access(email: str, subject: str) -> None:
    """Raise 403 when a teacher has not been assigned this subject."""
    allowed = accessible_subjects(email)
    if allowed is not None and subject not in allowed:
        raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")


def filter_subjects(email: str, subjects: list[str]) -> list[str]:
    """Restrict a subject listing to what the principal may see."""
    allowed = accessible_subjects(email)
    if allowed is None:
        return list(subjects)
    return [subject for subject in subjects if subject in allowed]
