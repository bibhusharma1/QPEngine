"""TOTP cryptographic operations - secret generation, encryption, and TOTP verification."""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

import pyotp
from cryptography.fernet import Fernet, InvalidToken

from config.settings import QUES_TOTP_ENCRYPTION_KEY


class TOTPEncryptionError(Exception):
    pass


class TOTPDecryptionError(Exception):
    pass


def _get_fernet() -> Fernet:
    """Get Fernet instance for TOTP secret encryption."""
    if not QUES_TOTP_ENCRYPTION_KEY:
        raise TOTPEncryptionError("QUES_TOTP_ENCRYPTION_KEY is not configured")
    key = QUES_TOTP_ENCRYPTION_KEY.strip()
    # Accept a properly formatted Fernet key directly; otherwise derive one so
    # operators can supply any strong passphrase-style value.
    try:
        Fernet(key.encode())
        return Fernet(key.encode())
    except Exception:
        digest = hashlib.sha256(key.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(digest)
        try:
            Fernet(fernet_key)
            return Fernet(fernet_key)
        except Exception as e:
            raise TOTPEncryptionError(f"Invalid TOTP encryption key: {e}")


def generate_secret() -> str:
    """Generate a secure random base32 secret for TOTP."""
    return pyotp.random_base32()


def encrypt_secret(secret: str) -> str:
    """Encrypt TOTP secret before storing."""
    if not secret:
        raise TOTPEncryptionError("Cannot encrypt empty secret")
    fernet = _get_fernet()
    encrypted = fernet.encrypt(secret.encode())
    return encrypted.decode()


def decrypt_secret(encrypted_secret: str) -> str:
    """Decrypt TOTP secret from storage."""
    if not encrypted_secret:
        raise TOTPDecryptionError("Cannot decrypt empty secret")
    fernet = _get_fernet()
    try:
        decrypted = fernet.decrypt(encrypted_secret.encode())
        return decrypted.decode()
    except InvalidToken:
        raise TOTPDecryptionError("Failed to decrypt TOTP secret - invalid key or corrupted data")
    except Exception as e:
        raise TOTPDecryptionError(f"Failed to decrypt TOTP secret: {e}")


def generate_provisioning_uri(secret: str, email: str, issuer_name: str = "Smart QPG") -> str:
    """Generate provisioning URI for authenticator apps."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer_name)


def verify_totp_code(secret: str, code: str, valid_window: int = 1) -> bool:
    """Verify TOTP code with clock drift tolerance."""
    return verify_totp_code_with_step(secret, code, valid_window=valid_window) is not None


def verify_totp_code_with_step(secret: str, code: str, valid_window: int = 1) -> int | None:
    """Verify a code and return the 30-second time step it matched, or None.

    The matched step powers replay protection: callers compare it against
    users.totp_last_used_step so a code cannot be consumed twice inside its
    validity window.
    """
    if not code or not code.strip():
        return None
    code = code.strip()
    totp = pyotp.TOTP(secret)
    now = datetime.now(timezone.utc)
    current_step = totp.timecode(now)
    for offset in range(-valid_window, valid_window + 1):
        if totp.at(now, offset) == code:
            return current_step + offset
    return None
