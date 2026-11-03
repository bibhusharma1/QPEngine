"""TOTP service layer - enrollment, verification, and authentication workflows."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from config.settings import (
    TOTP_ISSUER,
    TOTP_CHALLENGE_TTL_MINUTES,
    TOTP_FAILED_ATTEMPT_THRESHOLD,
    TOTP_COOLDOWN_SECONDS,
)
from src.masterdeorca import database
from src.masterdeorca.security import issue_token
from . import crypto, exceptions, qr


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def get_user_totp_info(email: str) -> dict:
    """Get TOTP information for a user."""
    with database.connect() as db:
        row = db.execute(
            """SELECT totp_enabled, totp_enrolled, totp_secret_encrypted, totp_pending_secret_encrypted,
                      totp_failed_attempts, totp_cooldown_until, totp_last_used_step
               FROM users WHERE email=?""",
            (email.lower(),),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(row)


def check_cooldown(email: str) -> None:
    """Check if user is in cooldown period."""
    info = get_user_totp_info(email)
    cooldown_until = info.get("totp_cooldown_until")
    if not cooldown_until:
        return
    try:
        cooldown_dt = datetime.fromisoformat(str(cooldown_until).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        # A corrupted timestamp must not silently disable lockout enforcement
        # forever; clear it and treat the cooldown as elapsed.
        with database.connect() as db:
            db.execute("UPDATE users SET totp_cooldown_until=NULL WHERE email=?", (email.lower(),))
        return
    if cooldown_dt > _now():
        retry_after = int((cooldown_dt - _now()).total_seconds()) + 1
        raise exceptions.TOTPCooldownError(max(retry_after, 1))


def start_enrollment(email: str, *, current_code: str | None = None) -> dict:
    """Return the permanent secret/QR; generate once and never rotate."""
    email = email.lower()
    check_cooldown(email)
    info = get_user_totp_info(email)

    secret = None
    if info.get("totp_secret_encrypted"):
        try:
            secret = crypto.decrypt_secret(info["totp_secret_encrypted"])
        except Exception:
            secret = None
    if not secret and info.get("totp_pending_secret_encrypted"):
        try:
            secret = crypto.decrypt_secret(info["totp_pending_secret_encrypted"])
        except Exception:
            secret = None

    if not secret:
        secret = crypto.generate_secret()
        encrypted_secret = crypto.encrypt_secret(secret)
        with database.connect() as db:
            db.execute(
                """UPDATE users SET totp_secret_encrypted=?, totp_pending_secret_encrypted=NULL,
                                  totp_enrolled=1
                   WHERE email=?""",
                (encrypted_secret, email),
            )

    provisioning_uri = crypto.generate_provisioning_uri(secret, email, TOTP_ISSUER)
    return {
        "provisioning_uri": provisioning_uri,
        "qr_code_payload": provisioning_uri,
        "qr_code_png_base64": qr.provisioning_qr_png_base64(provisioning_uri),
    }


def enrollment_qr_png(email: str) -> bytes:
    """PNG bytes of the QR code for the permanent secret."""
    email = email.lower()
    info = get_user_totp_info(email)
    secret = None
    if info.get("totp_secret_encrypted"):
        try:
            secret = crypto.decrypt_secret(info["totp_secret_encrypted"])
        except Exception:
            secret = None
    if not secret and info.get("totp_pending_secret_encrypted"):
        try:
            secret = crypto.decrypt_secret(info["totp_pending_secret_encrypted"])
        except Exception:
            secret = None
    if not secret:
        raise exceptions.TOTPNotEnrolledError("TOTP enrollment not started")
    return qr.provisioning_qr_png(crypto.generate_provisioning_uri(secret, email, TOTP_ISSUER))


def confirm_enrollment(email: str, code: str) -> dict:
    """Confirm TOTP enrollment with a valid code for the permanent secret."""
    email = email.lower()
    check_cooldown(email)

    info = get_user_totp_info(email)
    secret = None
    if info.get("totp_secret_encrypted"):
        try:
            secret = crypto.decrypt_secret(info["totp_secret_encrypted"])
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to decrypt TOTP secret") from exc
    if not secret and info.get("totp_pending_secret_encrypted"):
        try:
            secret = crypto.decrypt_secret(info["totp_pending_secret_encrypted"])
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to decrypt TOTP secret") from exc

    if not secret:
        raise exceptions.TOTPNotEnrolledError("TOTP enrollment not started")

    step = _verify_code_with_replay(info, secret, code)
    if step is None:
        _increment_failed_attempts(email)
        raise exceptions.InvalidTOTPCodeError("Invalid TOTP code")

    with database.connect() as db:
        db.execute(
            """UPDATE users SET totp_secret_encrypted=?, totp_pending_secret_encrypted=NULL,
                              totp_enabled=1, totp_enrolled=1,
                              totp_failed_attempts=0, totp_cooldown_until=NULL, totp_last_used_step=?
               WHERE email=?""",
            (crypto.encrypt_secret(secret), str(step), email),
        )

    return {"message": "TOTP has been enabled successfully", "totp_enabled": True}


def _verify_code_with_replay(info: dict, secret: str, code: str) -> int | None:
    """Verify a code and return its matched time step, rejecting step replays."""
    step = crypto.verify_totp_code_with_step(secret, code)
    if step is None:
        return None
    if str(info.get("totp_last_used_step") or "") == str(step):
        raise exceptions.TOTPReplayError("This TOTP code was already used; wait for the next code")
    return step


def _increment_failed_attempts(email: str) -> None:
    """Atomically increment failed attempts and apply cooldown at the threshold."""
    email = email.lower()
    cooldown_until = (_now() + timedelta(seconds=TOTP_COOLDOWN_SECONDS)).isoformat()
    with database.connect() as db:
        db.execute(
            """UPDATE users SET totp_failed_attempts = totp_failed_attempts + 1,
                              totp_cooldown_until = CASE
                                  WHEN totp_failed_attempts + 1 >= ? THEN ?
                                  ELSE totp_cooldown_until END
               WHERE email=?""",
            (TOTP_FAILED_ATTEMPT_THRESHOLD, cooldown_until, email),
        )


def create_login_challenge(email: str) -> str:
    """Create a short-lived login challenge for a TOTP-enabled user."""
    email = email.lower()
    challenge_token = secrets.token_urlsafe(32)
    expires_at = (_now() + timedelta(minutes=TOTP_CHALLENGE_TTL_MINUTES)).isoformat()
    now = _now_iso()
    with database.connect() as db:
        # Housekeeping: drop consumed and expired challenges, then cap live
        # challenges per user so the table cannot grow unboundedly.
        db.execute("DELETE FROM totp_login_challenges WHERE used=1 OR expires_at < ?", (now,))
        db.execute(
            """DELETE FROM totp_login_challenges WHERE user_email=? AND challenge_token NOT IN
               (SELECT challenge_token FROM totp_login_challenges WHERE user_email=?
                ORDER BY created_at DESC LIMIT 5)""",
            (email, email))
        db.execute(
            """INSERT INTO totp_login_challenges (challenge_token, user_email, expires_at, used, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (challenge_token, email, expires_at, now))
    return challenge_token


def verify_login_challenge(challenge_token: str, code: str) -> str:
    """Verify login challenge and issue JWT if successful."""
    with database.connect() as db:
        row = db.execute(
            """SELECT user_email, expires_at, used FROM totp_login_challenges
               WHERE challenge_token=?""",
            (challenge_token,),
        ).fetchone()

        if not row:
            raise exceptions.InvalidChallengeTokenError("Invalid challenge token")

        try:
            expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise exceptions.ExpiredChallengeTokenError("Challenge token has expired")
        if expires_at < _now():
            raise exceptions.ExpiredChallengeTokenError("Challenge token has expired")

        # Claim the challenge atomically so concurrent requests cannot both win.
        claimed = db.execute(
            "UPDATE totp_login_challenges SET used=1 WHERE challenge_token=? AND used=0",
            (challenge_token,)).rowcount
        if not claimed:
            raise exceptions.ReusedChallengeTokenError("Challenge token has already been used")
        email = row["user_email"]

    check_cooldown(email)

    info = get_user_totp_info(email)
    if not info.get("totp_enabled") or not info.get("totp_secret_encrypted"):
        raise exceptions.TOTPNotEnabledError("TOTP is not enabled for this user")

    try:
        secret = crypto.decrypt_secret(info["totp_secret_encrypted"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to decrypt TOTP secret") from exc

    step = _verify_code_with_replay(info, secret, code)
    if step is None:
        _increment_failed_attempts(email)
        raise exceptions.InvalidTOTPCodeError("Invalid TOTP code")

    with database.connect() as db:
        db.execute(
            "UPDATE users SET totp_failed_attempts=0, totp_cooldown_until=NULL, totp_last_used_step=? WHERE email=?",
            (str(step), email))

    # Issue normal JWT
    return issue_token(email)


def get_totp_status(email: str) -> dict:
    """Get TOTP status for a user."""
    email = email.lower()
    info = get_user_totp_info(email)
    return {
        "totp_enabled": bool(info.get("totp_enabled")),
        "enrolled": bool(info.get("totp_enrolled")),
    }


def disable_totp(email: str, code: str) -> dict:
    """Disable TOTP verification only - keep the secret."""
    email = email.lower()
    check_cooldown(email)

    info = get_user_totp_info(email)
    if not info.get("totp_enabled") or not info.get("totp_secret_encrypted"):
        raise exceptions.TOTPNotEnabledError("TOTP is not enabled")

    try:
        secret = crypto.decrypt_secret(info["totp_secret_encrypted"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to decrypt TOTP secret") from exc

    step = _verify_code_with_replay(info, secret, code)
    if step is None:
        _increment_failed_attempts(email)
        raise exceptions.InvalidTOTPCodeError("Invalid TOTP code")

    with database.connect() as db:
        db.execute(
            """UPDATE users SET totp_enabled=0, totp_failed_attempts=0,
                              totp_cooldown_until=NULL, totp_last_used_step=NULL
               WHERE email=?""",
            (email,),
        )

    return {"message": "TOTP has been disabled successfully", "totp_enabled": False}
