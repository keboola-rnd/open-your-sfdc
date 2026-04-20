#!/usr/bin/env python3
"""
Fetch Salesforce object descriptions and update the local SQLite database.

Connects to SF via describe(), grabs the description field for each object,
and adds/updates a `description` column in the _sf_objects table.

Usage:
    python scripts/fetch_descriptions.py
    python scripts/fetch_descriptions.py --db data/salesforce_full.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"

from scripts.sf_connect import get_connection  # noqa: E402

DEFAULT_DB_PATH = DATA_DIR / "salesforce_full.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    if not args.db.exists():
        print(f"Error: Database {args.db} not found.")
        return 1

    # Connect to Salesforce.
    print("Connecting to Salesforce...")
    sf = get_connection()
    print(f"Connected to {sf.sf_instance}")

    # Get global describe - contains name + label for all objects.
    print("Fetching global describe...")
    global_desc = sf.describe()
    sobjects = global_desc.get("sobjects", [])
    print(f"Found {len(sobjects)} objects in org")

    # Build name -> basic info from global describe.
    # Global describe does NOT have descriptions - we need per-object describe.
    # But per-object describe is expensive (1 API call per object).
    # Strategy: fetch describe only for objects we have in our DB.

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    # Add description column if missing.
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(_sf_objects)").fetchall()]
    if "description" not in cols:
        conn.execute("ALTER TABLE _sf_objects ADD COLUMN description TEXT")
        conn.commit()
        print("Added 'description' column to _sf_objects")

    # Get list of objects in our DB.
    db_objects = [r["name"] for r in conn.execute("SELECT name FROM _sf_objects").fetchall()]
    print(f"Our DB has {len(db_objects)} objects")

    # Fetch descriptions in batches.
    updated = 0
    errors = 0
    total = len(db_objects)

    for i, obj_name in enumerate(db_objects):
        if (i + 1) % 50 == 0 or i == 0:
            print(f"  [{i + 1}/{total}] Fetching descriptions...")

        try:
            desc = getattr(sf, obj_name).describe()
            description = (desc.get("description") or "").strip()
            label_plural = (desc.get("labelPlural") or "").strip()

            # Update DB.
            conn.execute(
                "UPDATE _sf_objects SET description = ? WHERE name = ?",
                (description, obj_name),
            )
            if description:
                updated += 1
        except Exception as exc:
            errors += 1
            if errors <= 5:
                print(f"  ! {obj_name}: {exc}")

    conn.commit()
    conn.close()

    print(f"\nDone. Updated {updated} descriptions, {errors} errors, {total} total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
