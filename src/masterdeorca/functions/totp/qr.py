"""Server-side QR rendering for TOTP provisioning URIs."""
from __future__ import annotations

import base64
import io

import qrcode


def provisioning_qr_png(provisioning_uri: str, *, box_size: int = 8, border: int = 2) -> bytes:
    """Render a provisioning URI as PNG bytes (black modules on white)."""
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=box_size, border=border)
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def provisioning_qr_png_base64(provisioning_uri: str) -> str:
    """Base64-encoded PNG of the provisioning QR code, for JSON responses."""
    return base64.b64encode(provisioning_qr_png(provisioning_uri)).decode("ascii")
