#!/usr/bin/env python3
"""
Export Salesforce objects that failed during the universal export.

Handles objects with special query requirements:
  - ContentDocumentLink: requires filter on ContentDocumentId (query per batch)
  - ContentFolderItem: requires filter on ParentContentFolderId
  - ContentFolderMember: requires filter on ChildRecordId
  - EventWhoRelation: not supported by Bulk API (force REST)
  - TaskWhoRelation: not supported by Bulk API (force REST)

Writes CSV files to the same export directory and updates metadata.json.

Usage:
    python scripts/export_gaps.py
    python scripts/export_gaps.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"

from scripts.sf_connect import flatten_record, get_connection  # noqa: E402

EXPORT_DIR = DATA_DIR / "sf_full_export"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_object_fields(sf, object_name: str) -> list[dict]:
    """Get field metadata for an object via describe()."""
    desc = getattr(sf, object_name).describe()
    compound_types = {"address", "location"}
    fields = []
    for f in desc["fields"]:
        if f["type"] in compound_types:
            continue
        fields.append({
            "name": f["name"],
            "type": f["type"],
            "label": f["label"],
            "length": f.get("length", 0),
            "referenceTo": f.get("referenceTo", []),
            "relationshipName": f.get("relationshipName"),
            "custom": f.get("custom", False),
            "nillable": f.get("nillable", True),
        })
    return fields


def export_via_rest_query_all(
    sf,
    object_name: str,
    fields: list[dict],
    *,
    where_clause: str = "",
    dry_run: bool = False,
) -> tuple[int, list[dict]]:
    """Export an object using REST query_all (no Bulk API)."""
    field_names = [f["name"] for f in fields]
    soql = f"SELECT {', '.join(field_names)} FROM {object_name}"
    if where_clause:
        soql += f" WHERE {where_clause}"

    if dry_run:
        # Just get count.
        count_soql = f"SELECT COUNT() FROM {object_name}"
        if where_clause:
            count_soql += f" WHERE {where_clause}"
        result = sf.query(count_soql)
        count = result.get("totalSize", 0)
        print(f"  {object_name}: {count:,} records (dry-run)")
        return count, []

    print(f"  Querying {object_name}...")
    start = time.time()
    result = sf.query_all(soql)
    records = result.get("records", [])
    elapsed = time.time() - start
    print(f"  {object_name}: {len(records):,} records in {elapsed:.1f}s")

    # Flatten and write CSV.
    if records:
        flat_records = [flatten_record(r) for r in records]
        csv_path = EXPORT_DIR / f"{object_name}.csv"
        all_keys = list(dict.fromkeys(k for row in flat_records for k in row.keys()))
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat_records)
        print(f"  Written to {csv_path}")

    return len(records), records


def export_content_document_links(
    sf,
    fields: list[dict],
    *,
    dry_run: bool = False,
) -> tuple[int, list[dict]]:
    """Export ContentDocumentLink by querying per ContentDocumentId batch."""
    print("\n  ContentDocumentLink requires per-ID filter. Gathering ContentDocumentIds...")

    # Get all ContentDocument IDs.
    result = sf.query_all("SELECT Id FROM ContentDocument")
    doc_ids = [r["Id"] for r in result.get("records", [])]
    print(f"  Found {len(doc_ids)} ContentDocument records")

    if not doc_ids:
        return 0, []

    if dry_run:
        # Estimate count.
        sample = doc_ids[:5]
        placeholders = "', '".join(sample)
        count_result = sf.query(
            f"SELECT COUNT() FROM ContentDocumentLink WHERE ContentDocumentId IN ('{placeholders}')"
        )
        est = count_result.get("totalSize", 0)
        est_total = int(est * len(doc_ids) / len(sample)) if sample else 0
        print(f"  Estimated ~{est_total:,} ContentDocumentLink records (dry-run)")
        return est_total, []

    # Query in batches of 100 IDs.
    field_names = [f["name"] for f in fields]
    all_records: list[dict] = []
    batch_size = 100

    for batch_start in range(0, len(doc_ids), batch_size):
        batch = doc_ids[batch_start:batch_start + batch_size]
        placeholders = "', '".join(batch)
        soql = (
            f"SELECT {', '.join(field_names)} FROM ContentDocumentLink "
            f"WHERE ContentDocumentId IN ('{placeholders}')"
        )
        result = sf.query_all(soql)
        batch_records = result.get("records", [])
        all_records.extend(batch_records)

        if (batch_start + batch_size) % 500 == 0:
            print(f"  ... {batch_start + batch_size}/{len(doc_ids)} docs queried, "
                  f"{len(all_records)} links found")

    print(f"  Total ContentDocumentLink: {len(all_records):,} records")

    # Write CSV.
    if all_records:
        flat_records = [flatten_record(r) for r in all_records]
        csv_path = EXPORT_DIR / "ContentDocumentLink.csv"
        all_keys = list(dict.fromkeys(k for row in flat_records for k in row.keys()))
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat_records)
        print(f"  Written to {csv_path}")

    return len(all_records), all_records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    print("Connecting to Salesforce...")
    sf = get_connection()
    print(f"Connected to {sf.sf_instance}")

    # Load existing metadata.
    metadata_path = EXPORT_DIR / "metadata.json"
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        with open(metadata_path) as f:
            metadata = json.load(f)

    results: dict[str, dict] = {}

    # 1. EventWhoRelation — force REST (Bulk API not supported).
    print("\n=== EventWhoRelation (REST, Bulk not supported) ===")
    try:
        fields = get_object_fields(sf, "EventWhoRelation")
        count, _ = export_via_rest_query_all(sf, "EventWhoRelation", fields, dry_run=args.dry_run)
        results["EventWhoRelation"] = {"status": "ok", "records": count, "fields": fields}
    except Exception as exc:
        print(f"  ! Error: {exc}")
        results["EventWhoRelation"] = {"status": "error", "error": str(exc)}

    # 2. TaskWhoRelation — force REST (Bulk API not supported).
    print("\n=== TaskWhoRelation (REST, Bulk not supported) ===")
    try:
        fields = get_object_fields(sf, "TaskWhoRelation")
        count, _ = export_via_rest_query_all(sf, "TaskWhoRelation", fields, dry_run=args.dry_run)
        results["TaskWhoRelation"] = {"status": "ok", "records": count, "fields": fields}
    except Exception as exc:
        print(f"  ! Error: {exc}")
        results["TaskWhoRelation"] = {"status": "error", "error": str(exc)}

    # 3. ContentDocumentLink — requires per-ID filter.
    print("\n=== ContentDocumentLink (per-ContentDocumentId filter) ===")
    try:
        fields = get_object_fields(sf, "ContentDocumentLink")
        count, _ = export_content_document_links(sf, fields, dry_run=args.dry_run)
        results["ContentDocumentLink"] = {"status": "ok", "records": count, "fields": fields}
    except Exception as exc:
        print(f"  ! Error: {exc}")
        results["ContentDocumentLink"] = {"status": "error", "error": str(exc)}

    # 4. ContentFolderItem — requires per-ID filter.
    print("\n=== ContentFolderItem (per-ParentContentFolderId filter) ===")
    try:
        fields = get_object_fields(sf, "ContentFolderItem")
        # Get all folder IDs first.
        try:
            folder_result = sf.query_all("SELECT Id FROM ContentFolder")
            folder_ids = [r["Id"] for r in folder_result.get("records", [])]
        except Exception:
            folder_ids = []
        if folder_ids:
            placeholders = "', '".join(folder_ids)
            where = f"ParentContentFolderId IN ('{placeholders}')"
            count, _ = export_via_rest_query_all(sf, "ContentFolderItem", fields,
                                                  where_clause=where, dry_run=args.dry_run)
        else:
            print("  No ContentFolder records found, skipping.")
            count = 0
        results["ContentFolderItem"] = {"status": "ok", "records": count, "fields": fields}
    except Exception as exc:
        print(f"  ! Error: {exc}")
        results["ContentFolderItem"] = {"status": "error", "error": str(exc)}

    # 5. ContentFolderMember — requires per-ID filter.
    print("\n=== ContentFolderMember (per-ParentContentFolderId filter) ===")
    try:
        fields = get_object_fields(sf, "ContentFolderMember")
        if folder_ids:
            placeholders = "', '".join(folder_ids)
            where = f"ParentContentFolderId IN ('{placeholders}')"
            count, _ = export_via_rest_query_all(sf, "ContentFolderMember", fields,
                                                  where_clause=where, dry_run=args.dry_run)
        else:
            print("  No ContentFolder records found, skipping.")
            count = 0
        results["ContentFolderMember"] = {"status": "ok", "records": count, "fields": fields}
    except Exception as exc:
        print(f"  ! Error: {exc}")
        results["ContentFolderMember"] = {"status": "error", "error": str(exc)}

    # Update metadata with new objects.
    if not args.dry_run:
        for obj_name, info in results.items():
            if info["status"] == "ok" and "fields" in info:
                metadata.setdefault("objects", {})[obj_name] = {
                    "name": obj_name,
                    "label": obj_name,
                    "custom": False,
                    "key_prefix": "",
                    "record_count": info["records"],
                    "fields": info["fields"],
                }
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)
        print(f"\nMetadata updated: {metadata_path}")

    # Summary.
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for obj_name, info in results.items():
        status = info["status"]
        if status == "ok":
            print(f"  {obj_name}: {info['records']:,} records")
        else:
            print(f"  {obj_name}: ERROR - {info.get('error', '?')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
