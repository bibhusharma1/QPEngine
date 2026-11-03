"""Placeholder script for future local-to-cloud SQLite/Turso synchronization."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=settings.DATABASE_PATH)
    parser.add_argument("--turso-url", type=str)
    parser.add_argument("--turso-token", type=str)
    parser.add_argument("--dry-run", action="store_true", default=True)
    args = parser.parse_args()

    print("SQLite/Turso synchronization script (template)")
    print(f"Source: {args.source_db}")
    print("This script provides the structure for future sync. Implement actual sync logic as needed.")
    print("Tables to sync: question_bank_records, questions, documents, subjects, jobs,")
    print("question_priorities, blueprints, paper_drafts, generated_papers, basic_configurations,")
    print("ui_configurations, reconciliation_state, etc.")


if __name__ == "__main__":
    main()
