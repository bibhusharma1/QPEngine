"""Bearer token authentication for the initial single-admin API."""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.settings import JWT_SECRET, JWT_TTL_MINUTES
from src.masterdeorca import database
from src.masterdeorca.roles import ADMIN_ROLE, TEACHER_ROLE, normalize_stored_role, validate_role

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
        if not row or row["status"] not in {"active", "approved", "authorized"}:
            raise HTTPException(status_code=401, detail="User account is not active", headers={"WWW-Authenticate": "Bearer"})
        return email
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from exc


def require_admin(email: str = Depends(require_user)) -> str:
    with database.connect() as db:
        row = db.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
    if not row or normalize_stored_role(row["role"]) != ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Admin role required")
    return email


def require_syllabus_manager(email: str = Depends(require_user)) -> str:
    """Syllabus lifecycle changes are limited to administrators."""
    with database.connect() as db:
        row = db.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
    if not row or normalize_stored_role(row["role"]) != ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Admin role required")
    return email


def require_syllabus_uploader(email: str = Depends(require_user)) -> str:
    """Allow admins and authenticated teachers to request uploads.

    Non-admins are still checked against the selected subject assignment by
    the syllabus route before any file is persisted.
    """
    with database.connect() as db:
        row = db.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Admin or assigned teacher role required")
    try:
        validate_role(row["role"])
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Only admins or teachers can upload syllabi") from exc
    return email


def require_write_user(email: str = Depends(require_user)) -> str:
    """Gate record-mutating routes."""
    return email


def require_admin_write(email: str = Depends(require_admin)) -> str:
    """Gate administrative mutations."""
    return email


def _user_role(email: str) -> str:
    with database.connect() as db:
        row = db.execute("SELECT role FROM users WHERE email=?", (email,)).fetchone()
    return normalize_stored_role(row["role"]) if row else TEACHER_ROLE


def accessible_subjects(email: str) -> set[str] | None:
    """Subject names the principal may access; None means unrestricted (admin).

    Read from the database on every call so assignment changes take effect
    immediately, without re-login or token claims.
    """
    role = _user_role(email)
    if role == ADMIN_ROLE:
        return None
    if role != TEACHER_ROLE:
        return set()
    return set(database.get_teacher_subjects(email))


def subject_allowed(email: str, subject_id: str | None) -> bool:
    """Read access: admins are unrestricted; teachers need the assignment.

    Generic resources (subject_id None) stay readable by every principal.
    """
    allowed = accessible_subjects(email)
    return allowed is None or subject_id is None or subject_id in allowed


def require_subject_access(email: str, subject: str) -> None:
    """Raise 403 when a non-admin has not been assigned this subject."""
    from src.masterdeorca import database
    user = database.get_user_by_email(email)
    if user and normalize_stored_role(user.get("role")) == ADMIN_ROLE:
        return
    allowed = accessible_subjects(email)
    # Also check new assignment system
    try:
        user_subjects = database.get_user_subjects_by_email(email)
        assigned_set = {u["subject_name"] for u in user_subjects}
        if assigned_set:
            if subject not in assigned_set:
                raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")
            return
    except Exception:
        pass
    if allowed is not None and subject not in allowed:
        raise HTTPException(status_code=403, detail="Subject access is not assigned to this account")


def filter_subjects(email: str, subjects: list[str]) -> list[str]:
    """Restrict a subject listing to what the principal may see."""
    from src.masterdeorca import database
    user = database.get_user_by_email(email)
    if user and normalize_stored_role(user.get("role")) == ADMIN_ROLE:
        return list(subjects)
    try:
        user_subjects = database.get_user_subjects_by_email(email)
        assigned_set = {u["subject_name"] for u in user_subjects}
        if assigned_set:
            return [s for s in subjects if s in assigned_set]
    except Exception:
        pass
    allowed = accessible_subjects(email)
    if allowed is None:
        return list(subjects)
    return [subject for subject in subjects if subject in allowed]
