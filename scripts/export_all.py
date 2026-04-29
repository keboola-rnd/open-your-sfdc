#!/usr/bin/env python3
"""
Universal Salesforce export - downloads all queryable objects to CSV files.

Discovers every queryable object in the org via sf.describe(), then for each
object fetches the full field list via describe() and downloads all records
(query_all for small objects, bulk API for large ones). Writes one CSV per
object plus a combined metadata.json with the full schema.

Usage:
    python scripts/export_all.py                     # Export everything
    python scripts/export_all.py --dry-run           # Show what would run
    python scripts/export_all.py --objects A,B,C     # Export only those
    python scripts/export_all.py --custom-only       # Only custom objects
    python scripts/export_all.py --resume            # Skip already exported
    python scripts/export_all.py --include-deleted   # Include soft-deleted
    python scripts/export_all.py --include-history   # Include *__History objects
                                                     # (field history — can be huge)

Requires .env with Salesforce credentials (see sf_connect.py).
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

# Make sibling ``scripts`` imports work when invoked as `python scripts/export_all.py`.
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"

from scripts.sf_connect import (  # noqa: E402
    flatten_record,
    get_connection,
)

try:
    from simple_salesforce import Salesforce
    from simple_salesforce.exceptions import SalesforceError
except ImportError:
    print("Error: simple-salesforce is not installed.")
    print("Run: pip install simple-salesforce")
    sys.exit(1)


EXPORT_DIR = DATA_DIR / "sf_full_export"

# Suffix patterns for system/auto-generated objects we always skip — these
# are never useful (Chatter feeds, record sharing, CDC streams, tags).
PERMANENT_SKIP_SUFFIXES = (
    "__Feed",
    "__Share",
    "__Tag",
    "__ChangeEvent",
)

# Suffix patterns that are skipped by default but can be re-enabled with
# ``--include-history``. Field history tables are often huge (millions of
# rows for big orgs) and keep 18 months of retention without Shield.
OPTIONAL_SKIP_SUFFIXES = (
    "__History",
)

# Hard-coded skip list for objects that are known to fail or return useless data.
# NOTE: ``SetupAuditTrail`` is NOT here — we always want it (180-day retention
# of "who changed what in Setup"). EventLogFile stays because its binaries
# need a separate endpoint; see scripts/download_event_logs.py.
SKIP_OBJECTS = {
    "IdeaComment",
    "Vote",
    "ContentBody",
    "EventLogFile",
    "ApexLog",
    # Extra noisy or unqueryable-in-practice system objects:
    "LoginHistory",
    "AuthSession",
    "FieldHistoryArchive",
    "RecordActionHistory",
    "AppTabMember",
    "ColorDefinition",
    "IconDefinition",
    "UserEntityAccess",
    "UserFieldAccess",
    "UserRecordAccess",
    "OwnedContentDocument",
    "TenantUsageEntitlement",
    "DataStatistics",
    "FlowVersionView",
    "FlowVariableView",
    "FormulaFunctionAllowedType",
    "FormulaFunctionCategory",
    "FormulaFunction",
    "PicklistValueInfo",
    "RelationshipDomain",
    "RelationshipInfo",
    "EntityParticle",
    "FieldDefinition",
    "DataType",
}

# SOQL selects cannot include these compound field types directly.
COMPOUND_FIELD_TYPES = {"address", "location"}

# Use bulk API when a single object has at least this many records.
BULK_THRESHOLD = 50_000

# Max character length of a SOQL query Salesforce will accept. We chunk field
# lists by this when an object has unusually many fields.
SOQL_QUERY_CHAR_LIMIT = 18_000

# Objects whose REST query refuses to run without a WHERE filter on a parent
# ID (Salesforce platform restriction, not a bug). We work around it with
# chunked queries: load parent IDs from the parent CSV (or fetch via API as a
# fallback) and run ``SELECT ... WHERE <filter_field> IN (chunk)``.
#
# ContentFolderMember is special: SF only accepts ``=`` here, not ``IN`` —
# hence chunk_size=1 (yields one query per parent folder).
#
# Alphabetical iteration in discover_objects() guarantees the parent runs
# first for the default full-export case (ContentDocument < ContentDocumentLink,
# ContentFolder < ContentFolderItem/Member). For ad-hoc --objects runs the
# helper falls back to a live ``SELECT Id FROM <parent>`` query.
#
# Format: child_object -> (parent_object, filter_field, parent_id_column, chunk_size).
FILTER_REQUIRED_OBJECTS: dict[str, tuple[str, str, str, int]] = {
    "ContentDocumentLink": ("ContentDocument", "ContentDocumentId", "Id", 200),
    "ContentFolderItem": ("ContentFolder", "ParentContentFolderId", "Id", 200),
    "ContentFolderMember": ("ContentFolder", "ParentContentFolderId", "Id", 1),
}

# Objects the Bulk API rejects with "InvalidEntity" (typically polymorphic
# junction tables tying Event/Task to Lead/Contact/User). Force REST
# query_all regardless of record count.
FORCE_REST_OBJECTS: set[str] = {
    "EventWhoRelation",
    "TaskWhoRelation",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_exportable(obj: dict, *, include_history: bool = False) -> bool:
    """Return True if the SObject describe entry represents something we want to export."""
    name = obj.get("name", "")
    if not obj.get("queryable"):
        return False
    if not obj.get("retrieveable", True):
        return False
    if name in SKIP_OBJECTS:
        return False
    if any(name.endswith(suffix) for suffix in PERMANENT_SKIP_SUFFIXES):
        return False
    if not include_history and any(
        name.endswith(suffix) for suffix in OPTIONAL_SKIP_SUFFIXES
    ):
        return False
    return True


def discover_objects(sf: Salesforce, *, include_history: bool = False) -> list[dict]:
    """Return the list of sobject describe entries we plan to export."""
    print("\nDiscovering objects via sf.describe()...")
    global_desc = sf.describe()
    sobjects = global_desc["sobjects"]
    exportable = [o for o in sobjects if is_exportable(o, include_history=include_history)]
    exportable.sort(key=lambda o: o["name"])
    history_note = " (+ __History)" if include_history else ""
    print(
        f"  Found {len(sobjects)} sobjects total, "
        f"{len(exportable)} exportable after filtering{history_note}."
    )
    return exportable


def describe_object(sf: Salesforce, object_name: str) -> dict:
    """Call Object.describe() and return the raw describe dict."""
    return getattr(sf, object_name).describe()


def selectable_fields(desc: dict) -> list[dict]:
    """Return field metadata entries that can appear in a SOQL SELECT."""
    return [
        f
        for f in desc["fields"]
        if f.get("type") not in COMPOUND_FIELD_TYPES
    ]


def count_records(sf: Salesforce, object_name: str) -> int | None:
    """Return the record count for an object, or None on error."""
    try:
        result = sf.query(f"SELECT COUNT() FROM {object_name}")
        return int(result["totalSize"])
    except SalesforceError as exc:
        print(f"  Could not count {object_name}: {exc}")
        return None


def chunk_fields_for_soql(fields: list[str], object_name: str) -> list[list[str]]:
    """Split a long field list into chunks whose SOQL fits under the query limit.

    Almost every object fits in a single chunk; this only matters for objects
    with hundreds of fields.
    """
    base_overhead = len(f"SELECT  FROM {object_name}")
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = base_overhead
    for field in fields:
        # +2 for ", " separator
        projected = current_len + len(field) + 2
        if current and projected > SOQL_QUERY_CHAR_LIMIT:
            chunks.append(current)
            current = [field]
            current_len = base_overhead + len(field)
        else:
            current.append(field)
            current_len = projected
    if current:
        chunks.append(current)
    return chunks


def query_all_standard(
    sf: Salesforce,
    object_name: str,
    fields: list[str],
    include_deleted: bool,
) -> list[dict]:
    """Fetch records using the standard REST query_all endpoint."""
    chunks = chunk_fields_for_soql(fields, object_name)
    if len(chunks) == 1:
        soql = f"SELECT {', '.join(chunks[0])} FROM {object_name}"
        result = sf.query_all(soql, include_deleted=include_deleted)
        return result["records"]

    # For very wide objects: fetch Id separately per chunk and merge by Id.
    merged: dict[str, dict] = {}
    for chunk in chunks:
        chunk_fields = chunk if "Id" in chunk else ["Id"] + chunk
        soql = f"SELECT {', '.join(chunk_fields)} FROM {object_name}"
        result = sf.query_all(soql, include_deleted=include_deleted)
        for rec in result["records"]:
            rid = rec.get("Id")
            if not rid:
                continue
            merged.setdefault(rid, {}).update(rec)
    return list(merged.values())


def query_all_bulk(
    sf: Salesforce,
    object_name: str,
    fields: list[str],
) -> list[dict]:
    """Fetch records via the Bulk API. Much faster for very large objects."""
    bulk_handle = getattr(sf.bulk, object_name)
    soql = f"SELECT {', '.join(fields)} FROM {object_name}"
    # simple_salesforce returns a list of dicts for bulk query_all.
    return list(bulk_handle.query_all(soql))


def get_parent_ids(
    sf: Salesforce,
    parent_object: str,
    parent_csv: Path,
    parent_id_column: str = "Id",
) -> list[str]:
    """Return parent IDs for a chunked child query.

    Reads from the local parent CSV when present (the common case during a
    full export, since alphabetical order means the parent ran first). Falls
    back to ``SELECT Id FROM <parent_object>`` when the CSV is missing — this
    matters for ad-hoc ``--objects ContentDocumentLink`` runs.
    """
    if parent_csv.exists() and parent_csv.stat().st_size > 0:
        ids: list[str] = []
        with open(parent_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = row.get(parent_id_column)
                if pid:
                    ids.append(pid)
        return ids

    print(
        f"    parent CSV {parent_csv.name} missing -> "
        f"querying {parent_object}.{parent_id_column} from API"
    )
    result = sf.query_all(f"SELECT {parent_id_column} FROM {parent_object}")
    return [rec[parent_id_column] for rec in result["records"] if rec.get(parent_id_column)]


def query_chunked_by_parent_ids(
    sf: Salesforce,
    object_name: str,
    fields: list[str],
    filter_field: str,
    parent_ids: list[str],
    chunk_size: int,
) -> list[dict]:
    """Fetch records that require a parent-ID filter, one chunk at a time.

    Uses ``WHERE filter_field IN (chunk)`` for chunk_size > 1 and
    ``WHERE filter_field = 'id'`` for chunk_size == 1 (some objects, notably
    ContentFolderMember, only accept the equals operator).
    """
    if not parent_ids:
        return []

    field_clause = ", ".join(fields)
    merged: dict[str, dict] = {}
    total_chunks = (len(parent_ids) + chunk_size - 1) // chunk_size

    for idx, start in enumerate(range(0, len(parent_ids), chunk_size), start=1):
        chunk = parent_ids[start:start + chunk_size]
        if len(chunk) == 1:
            where = f"{filter_field} = '{chunk[0]}'"
        else:
            ids_quoted = ", ".join(f"'{pid}'" for pid in chunk)
            where = f"{filter_field} IN ({ids_quoted})"
        soql = f"SELECT {field_clause} FROM {object_name} WHERE {where}"
        result = sf.query_all(soql)
        for rec in result["records"]:
            rid = rec.get("Id")
            if rid:
                merged[rid] = rec
        # Light progress logging for runs with many chunks.
        if total_chunks >= 10 and idx % 10 == 0:
            print(f"    chunk {idx}/{total_chunks} -> {len(merged):,} unique records so far")

    return list(merged.values())


def write_csv(records: list[dict], path: Path, fields: list[str]) -> int:
    """Write records to CSV. Columns follow the provided field list order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        # Still create an empty CSV with just the header so downstream tools
        # know the object exists but has no data.
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
        return 0

    flat_records = [flatten_record(rec) for rec in records]
    # Collect all keys we actually saw so nested lookup fields make it in.
    seen_keys: set[str] = set()
    for r in flat_records:
        seen_keys.update(r.keys())
    # Keep field order: declared fields first, then any extras alphabetically.
    extras = sorted(k for k in seen_keys if k not in fields)
    columns = list(fields) + extras

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in flat_records:
            writer.writerow(row)
    return len(flat_records)


def extract_field_metadata(field: dict) -> dict[str, Any]:
    """Pull the metadata fields we want to persist for each field."""
    return {
        "name": field.get("name"),
        "label": field.get("label"),
        "type": field.get("type"),
        "length": field.get("length"),
        "precision": field.get("precision"),
        "scale": field.get("scale"),
        "nillable": field.get("nillable"),
        "custom": field.get("custom"),
        "referenceTo": field.get("referenceTo", []),
        "relationshipName": field.get("relationshipName"),
        "picklistValues": [
            {"value": pv.get("value"), "label": pv.get("label"), "active": pv.get("active")}
            for pv in field.get("picklistValues", [])
        ],
    }


def report_limits(sf: Salesforce) -> dict:
    """Return current API limits (best effort)."""
    try:
        return sf.restful("limits") or {}
    except Exception as exc:  # noqa: BLE001
        print(f"  Could not fetch limits: {exc}")
        return {}


def api_calls_used(limits: dict) -> int | None:
    """Extract API calls used from a limits response, if present."""
    usage = limits.get("DailyApiRequests")
    if not usage:
        return None
    try:
        return int(usage.get("Max", 0)) - int(usage.get("Remaining", 0))
    except (TypeError, ValueError):
        return None


def export_object(
    sf: Salesforce,
    obj_describe_entry: dict,
    export_dir: Path,
    *,
    include_deleted: bool,
    resume: bool,
) -> dict:
    """Export a single SObject. Returns a result dict for the export log."""
    object_name = obj_describe_entry["name"]
    csv_path = export_dir / f"{object_name}.csv"

    result: dict[str, Any] = {
        "object": object_name,
        "label": obj_describe_entry.get("label"),
        "custom": obj_describe_entry.get("custom", False),
        "status": "pending",
        "records": 0,
        "duration_sec": 0.0,
    }

    if resume and csv_path.exists() and csv_path.stat().st_size > 0:
        # Count data rows (minus header) cheaply.
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                rows = sum(1 for _ in f) - 1
        except OSError:
            rows = 0
        print(f"  [resume] skipping {object_name} ({max(rows, 0)} rows already exported)")
        result["status"] = "skipped"
        result["records"] = max(rows, 0)
        return result

    started = time.time()
    try:
        desc = describe_object(sf, object_name)
        fields_meta = selectable_fields(desc)
        field_names = [f["name"] for f in fields_meta]
        if not field_names:
            raise RuntimeError("no selectable fields")

        if object_name in FILTER_REQUIRED_OBJECTS:
            # Filter-required objects refuse SELECT without WHERE — chunked
            # query against the parent's IDs is the only way to get a full
            # dump. count_records() also fails for these, so we skip it.
            parent_object, filter_field, parent_id_column, chunk_size = (
                FILTER_REQUIRED_OBJECTS[object_name]
            )
            parent_csv_path = export_dir / f"{parent_object}.csv"
            parent_ids = get_parent_ids(
                sf, parent_object, parent_csv_path, parent_id_column
            )
            print(
                f"  {object_name}: chunked query via {filter_field} "
                f"over {len(parent_ids):,} {parent_object} parent IDs "
                f"(chunk_size={chunk_size})"
            )
            records = query_chunked_by_parent_ids(
                sf, object_name, field_names, filter_field, parent_ids, chunk_size,
            )
            result["method"] = "rest-chunked"
            result["record_count_pre"] = len(parent_ids)
        else:
            total = count_records(sf, object_name)
            result["record_count_pre"] = total

            if object_name in FORCE_REST_OBJECTS:
                print(f"  {object_name}: forced REST query_all (Bulk API rejects this entity)")
                records = query_all_standard(
                    sf, object_name, field_names, include_deleted=include_deleted
                )
                result["method"] = "rest-forced"
            elif total is not None and total >= BULK_THRESHOLD:
                print(f"  {object_name}: {total:,} records -> using bulk API")
                records = query_all_bulk(sf, object_name, field_names)
                result["method"] = "bulk"
            else:
                count_display = f"{total:,}" if total is not None else "unknown"
                print(f"  {object_name}: {count_display} records -> using REST query_all")
                records = query_all_standard(
                    sf, object_name, field_names, include_deleted=include_deleted
                )
                result["method"] = "rest"

        written = write_csv(records, csv_path, field_names)
        result["records"] = written
        result["status"] = "ok"

        # Attach field metadata for later emission in metadata.json.
        result["_fields_meta"] = [extract_field_metadata(f) for f in fields_meta]
        result["_describe_label"] = desc.get("label", object_name)
        result["_custom"] = desc.get("custom", False)
        result["_key_prefix"] = desc.get("keyPrefix")

    except SalesforceError as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  ! {object_name} failed: {result['error']}")
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  ! {object_name} failed: {result['error']}")

    result["duration_sec"] = round(time.time() - started, 2)
    return result


def write_metadata(
    export_dir: Path,
    sf_instance: str,
    object_results: list[dict],
) -> None:
    """Emit metadata.json with per-object schema information."""
    objects_meta: dict[str, dict] = {}
    for res in object_results:
        if res["status"] not in ("ok", "skipped"):
            continue
        fields_meta = res.pop("_fields_meta", None)
        if fields_meta is None:
            # Resume case: we don't have fresh describe data for this object.
            continue
        name = res["object"]
        objects_meta[name] = {
            "name": name,
            "label": res.pop("_describe_label", name),
            "custom": res.pop("_custom", False),
            "key_prefix": res.pop("_key_prefix", None),
            "record_count": res["records"],
            "fields": fields_meta,
        }

    metadata = {
        "export_date": utc_now_iso(),
        "sf_instance": sf_instance,
        "objects": objects_meta,
    }
    out = export_dir / "metadata.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nMetadata written to {out}")


def write_export_log(
    export_dir: Path,
    started_at: str,
    finished_at: str,
    object_results: list[dict],
    api_calls_before: int | None,
    api_calls_after: int | None,
) -> None:
    """Emit _export_log.json summarizing the run."""
    total_records = sum(r.get("records", 0) for r in object_results)
    errors = [
        f"{r['object']}: {r.get('error')}"
        for r in object_results
        if r.get("status") == "error"
    ]

    log: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "total_objects": len(object_results),
        "total_records": total_records,
        "objects": {
            r["object"]: {
                "status": r["status"],
                "records": r.get("records", 0),
                "duration_sec": r.get("duration_sec", 0.0),
                "method": r.get("method"),
                "error": r.get("error"),
            }
            for r in object_results
        },
        "errors": errors,
    }
    if api_calls_before is not None and api_calls_after is not None:
        log["api_calls_used"] = max(api_calls_after - api_calls_before, 0)

    out = export_dir / "_export_log.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
    print(f"Export log written to {out}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--objects",
        type=str,
        default=None,
        help="Comma-separated list of object API names to export (default: all).",
    )
    parser.add_argument(
        "--custom-only",
        action="store_true",
        help="Export only custom objects (name ends with __c).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be exported but do not query any records.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip objects that already have a non-empty CSV file.",
    )
    parser.add_argument(
        "--include-deleted",
        action="store_true",
        help="Include soft-deleted records (REST query_all only).",
    )
    parser.add_argument(
        "--include-history",
        action="store_true",
        help="Include *__History field-history objects (can add millions of rows).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="Seconds to sleep between objects (rate limiting, default 0.5).",
    )
    return parser.parse_args(argv)


def filter_objects(
    all_objects: list[dict],
    *,
    only_names: list[str] | None,
    custom_only: bool,
) -> list[dict]:
    selected = all_objects
    if only_names:
        wanted = {n.strip() for n in only_names if n.strip()}
        selected = [o for o in selected if o["name"] in wanted]
    if custom_only:
        selected = [o for o in selected if o["name"].endswith("__c")]
    return selected


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print("=" * 60)
    print("Salesforce Universal Export")
    print("=" * 60)

    print("\nConnecting to Salesforce...")
    sf = get_connection()
    print(f"Connected to: {sf.sf_instance}")

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    all_objects = discover_objects(sf, include_history=args.include_history)
    only_names = args.objects.split(",") if args.objects else None
    selected = filter_objects(
        all_objects,
        only_names=only_names,
        custom_only=args.custom_only,
    )

    if not selected:
        print("No objects matched the provided filters. Nothing to do.")
        return 0

    print(f"\nExport target: {EXPORT_DIR}")
    print(f"Selected {len(selected)} object(s):")
    for o in selected:
        marker = " (custom)" if o["name"].endswith("__c") else ""
        print(f"  - {o['name']}{marker}")

    if args.dry_run:
        print("\n[dry-run] exiting without fetching records.")
        return 0

    started_at = utc_now_iso()
    limits_before = report_limits(sf)
    api_before = api_calls_used(limits_before)

    object_results: list[dict] = []
    for idx, obj in enumerate(selected, start=1):
        print(f"\n[{idx}/{len(selected)}] Exporting {obj['name']}...")
        result = export_object(
            sf,
            obj,
            EXPORT_DIR,
            include_deleted=args.include_deleted,
            resume=args.resume,
        )
        object_results.append(result)

        # Light rate limiting between objects.
        if idx < len(selected) and args.sleep > 0:
            time.sleep(args.sleep)

    finished_at = utc_now_iso()
    limits_after = report_limits(sf)
    api_after = api_calls_used(limits_after)

    # metadata.json (pops internal _fields_meta keys -> must run before log).
    write_metadata(EXPORT_DIR, sf.sf_instance, object_results)
    write_export_log(
        EXPORT_DIR,
        started_at,
        finished_at,
        object_results,
        api_before,
        api_after,
    )

    ok = sum(1 for r in object_results if r["status"] == "ok")
    skipped = sum(1 for r in object_results if r["status"] == "skipped")
    errored = sum(1 for r in object_results if r["status"] == "error")
    total_records = sum(r.get("records", 0) for r in object_results)

    print("\n" + "=" * 60)
    print(f"Done. ok={ok} skipped={skipped} error={errored}")
    print(f"Total records: {total_records:,}")
    print(f"Output: {EXPORT_DIR}")
    print("\nNext step: python scripts/import_to_sqlite.py")

    return 0 if errored == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
