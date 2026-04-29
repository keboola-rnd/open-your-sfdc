#!/usr/bin/env python3
"""
Rebuild metadata.json and _export_log.json from existing CSV files.

Use this when the bookkeeping JSON files have become inconsistent with the
CSVs on disk — for example, after a partial ``export_all.py --objects ...``
run overwrote them with only a subset of objects. This script does NOT
re-export any data: it walks every ``*.csv`` in the export dir, calls
``describe()`` for field metadata, counts CSV rows for the record count,
and writes fresh metadata.json + _export_log.json.

Usage:
    python scripts/rebuild_metadata.py
    python scripts/rebuild_metadata.py --workers 8   # parallel describes
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"
EXPORT_DIR = DATA_DIR / "sf_full_export"

from scripts.export_all import (  # noqa: E402
    extract_field_metadata,
    selectable_fields,
    utc_now_iso,
)
from scripts.sf_connect import get_connection  # noqa: E402

try:
    from simple_salesforce import Salesforce
    from simple_salesforce.exceptions import SalesforceError
except ImportError:
    print("Error: simple-salesforce is not installed.")
    sys.exit(1)

# Salesforce text fields (ApexClass.Body, Task.Description, etc.) routinely
# exceed Python's default csv field cap of 128 KB. Bump it to the platform max.
csv.field_size_limit(sys.maxsize)


def list_export_objects(export_dir: Path) -> list[str]:
    """Return SObject API names for every ``*.csv`` in the export dir.

    Skips ``_*.csv`` (none today, but keeps the namespace open for
    bookkeeping files like ``_export_log.json`` if a future change adds CSV
    side-files).
    """
    return sorted(
        p.stem
        for p in export_dir.glob("*.csv")
        if not p.name.startswith("_")
    )


def count_csv_rows(csv_path: Path) -> int:
    """Count CSV records (not lines).

    A naive newline count over-reports any object with multi-line text fields
    — Task.Description with a single embedded newline doubles the count, etc.
    csv.reader handles quoted multi-line cells correctly, at a small CPU cost
    that still finishes the full export in under a minute.
    """
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return 0
    with open(csv_path, "r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        return sum(1 for _ in reader)


def describe_one(
    sf: Salesforce, object_name: str, csv_path: Path
) -> tuple[str, dict | None, dict, str | None]:
    """Describe a single object; return (name, meta_or_None, log_entry, err_or_None)."""
    rows = count_csv_rows(csv_path)
    try:
        desc = getattr(sf, object_name).describe()
    except SalesforceError as exc:
        err = f"{type(exc).__name__}: {exc}"
        return object_name, None, {
            "status": "error",
            "records": rows,
            "duration_sec": 0.0,
            "method": None,
            "error": err,
        }, err
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        return object_name, None, {
            "status": "error",
            "records": rows,
            "duration_sec": 0.0,
            "method": None,
            "error": err,
        }, err

    fields_meta = [extract_field_metadata(f) for f in selectable_fields(desc)]
    meta_entry = {
        "name": object_name,
        "label": desc.get("label", object_name),
        "custom": desc.get("custom", False),
        "key_prefix": desc.get("keyPrefix"),
        "record_count": rows,
        "fields": fields_meta,
    }
    log_entry = {
        "status": "ok",
        "records": rows,
        "duration_sec": 0.0,
        "method": "rebuilt-from-disk",
        "error": None,
    }
    return object_name, meta_entry, log_entry, None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel describe() calls (default 8). Set to 1 for serial.",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=EXPORT_DIR,
        help=f"Directory with the CSV files to scan (default: {EXPORT_DIR}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    export_dir: Path = args.export_dir

    print("=" * 60)
    print("Rebuild metadata.json + _export_log.json from existing CSVs")
    print("=" * 60)

    if not export_dir.exists():
        print(f"Export dir not found: {export_dir}")
        return 1

    object_names = list_export_objects(export_dir)
    if not object_names:
        print(f"No CSV files found in {export_dir}; nothing to do.")
        return 1

    print(f"\nFound {len(object_names)} CSV files in {export_dir}")
    print("Connecting to Salesforce...")
    sf = get_connection()
    print(f"Connected to: {sf.sf_instance}")
    print(f"Running describe() for {len(object_names)} objects with {args.workers} worker(s)...")

    objects_meta: dict[str, dict[str, Any]] = {}
    object_log: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    started_at = utc_now_iso()
    started = time.time()
    completed = 0

    if args.workers <= 1:
        for name in object_names:
            obj_name, meta, log_entry, err = describe_one(
                sf, name, export_dir / f"{name}.csv"
            )
            if meta is not None:
                objects_meta[obj_name] = meta
            object_log[obj_name] = log_entry
            if err:
                errors.append(f"{obj_name}: {err}")
            completed += 1
            if completed % 50 == 0 or completed == len(object_names):
                elapsed = time.time() - started
                rate = completed / max(elapsed, 0.001)
                eta = (len(object_names) - completed) / max(rate, 0.001)
                print(
                    f"  [{completed}/{len(object_names)}] "
                    f"elapsed {elapsed:.0f}s, ETA {eta:.0f}s, errors {len(errors)}"
                )
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    describe_one, sf, name, export_dir / f"{name}.csv"
                ): name
                for name in object_names
            }
            for fut in as_completed(futures):
                obj_name, meta, log_entry, err = fut.result()
                if meta is not None:
                    objects_meta[obj_name] = meta
                object_log[obj_name] = log_entry
                if err:
                    errors.append(f"{obj_name}: {err}")
                completed += 1
                if completed % 50 == 0 or completed == len(object_names):
                    elapsed = time.time() - started
                    rate = completed / max(elapsed, 0.001)
                    eta = (len(object_names) - completed) / max(rate, 0.001)
                    print(
                        f"  [{completed}/{len(object_names)}] "
                        f"elapsed {elapsed:.0f}s, ETA {eta:.0f}s, errors {len(errors)}"
                    )

    finished_at = utc_now_iso()

    metadata = {
        "export_date": finished_at,
        "sf_instance": sf.sf_instance,
        "objects": dict(sorted(objects_meta.items())),
        "rebuilt_from_disk": True,
    }
    metadata_path = export_dir / "metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nWrote metadata for {len(objects_meta)} objects -> {metadata_path}")

    total_records = sum(o.get("records", 0) for o in object_log.values())
    log = {
        "started_at": started_at,
        "finished_at": finished_at,
        "total_objects": len(object_names),
        "total_records": total_records,
        "objects": dict(sorted(object_log.items())),
        "errors": errors,
        "rebuilt_from_disk": True,
    }
    log_path = export_dir / "_export_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
    print(f"Wrote log -> {log_path}")

    elapsed = time.time() - started
    print(
        f"\nDone in {elapsed:.0f}s. "
        f"{len(objects_meta)} ok, {len(errors)} errors, "
        f"{total_records:,} total records."
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
