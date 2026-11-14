#!/usr/bin/env python3
"""Backward-compatible entry point for local-only administrator provisioning."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.create_local_admin import main


if __name__ == "__main__":
    raise SystemExit(main())
