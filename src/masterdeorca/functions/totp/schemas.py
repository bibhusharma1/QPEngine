"""TOTP request schemas."""
from __future__ import annotations

from pydantic import BaseModel, Field


class TOTPEnrollRequest(BaseModel):
    """Optional current code; required to re-enroll while TOTP is already enabled."""
    code: str | None = Field(default=None, min_length=6, max_length=6, pattern=r"^\d{6}$")


class TOTPConfirmRequest(BaseModel):
    """Request for confirming TOTP enrollment."""
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TOTPVerifyRequest(BaseModel):
    """Request for verifying TOTP during login."""
    challenge_token: str = Field(min_length=1)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TOTPDisableRequest(BaseModel):
    """Request for disabling TOTP."""
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
