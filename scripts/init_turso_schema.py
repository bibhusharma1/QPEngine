#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure repo root on path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.masterdeorca import database


def main() -> int:
    try:
        database.initialize()
        print("Turso schema initialized successfully")
        return 0
    except Exception as e:
        print(f"Error initializing schema: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
