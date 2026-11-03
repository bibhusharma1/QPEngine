"""
FastAPI auth micro‑service.

Run:  uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from db import check_credentials

app = FastAPI(title="Auth Service")

# Allow requests from any frontend origin (tighten in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── request / response models ─────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    success: bool
    message: str
    source: str  # 'turso' or 'local'


# ── routes ─────────────────────────────────────────────────────────────────
@app.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    """
    Validate credentials.
    Checks Turso DB first; if unavailable falls back to local SQLite.
    """
    is_valid, source = check_credentials(req.username, req.password)

    if is_valid:
        return LoginResponse(
            success=True,
            message="Login successful",
            source=source,
        )

    return LoginResponse(
        success=False,
        message="Invalid username or password",
        source=source,
    )


@app.get("/health")
def health():
    return {"status": "ok"}

