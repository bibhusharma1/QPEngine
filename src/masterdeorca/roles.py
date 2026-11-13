"""Canonical application roles and validation helpers."""

ADMIN_ROLE = "admin"
TEACHER_ROLE = "teacher"
ALLOWED_ROLES = frozenset({ADMIN_ROLE, TEACHER_ROLE})


def normalize_stored_role(value: object) -> str:
    """Map legacy stored role values to the two supported roles."""
    return ADMIN_ROLE if str(value or "").strip().casefold() == ADMIN_ROLE else TEACHER_ROLE


def validate_role(value: object) -> str:
    """Normalize a new API role and reject anything outside the public contract."""
    role = str(value or "").strip().casefold()
    if role not in ALLOWED_ROLES:
        raise ValueError("role must be either 'admin' or 'teacher'")
    return role
