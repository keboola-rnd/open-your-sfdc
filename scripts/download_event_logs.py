#!/usr/bin/env python3
"""
Download Salesforce EventLogFile binaries — who did what, when, via what API.

EventLogFile contains the hourly / daily audit stream:
  - API calls (URI, method, user, duration)
  - Logins (IP, browser, MFA status)
  - Apex executions, report exports, content downloads, admin actions

Retention without an Event Monitoring licence is **1 day** (so if you haven't
exported yesterday's logs, they are gone). With the licence it is 30 days,
configurable up to 1 year.

This script:
  1. Queries ``EventLogFile`` for every row in ``--days`` window (default 365).
  2. Detects whether the org has meaningful log data — emits a clear warning
     if the query returns nothing (most likely: no licence + retention expired).
  3. For each row, downloads the ``LogFile`` binary (CSV) into
     ``data/sf_event_logs/<YYYY-MM-DD>/<EventType>_<Interval>_<Sequence>.csv``.
  4. Writes ``manifest.json`` mapping EventLogFile.Id to local path + metadata.

Usage:
    python scripts/download_event_logs.py                   # Last 365 days
    python scripts/download_event_logs.py --days 7          # Only last week
    python scripts/download_event_logs.py --dry-run         # Just list
    python scripts/download_event_logs.py --resume          # Skip downloaded
    python scripts/download_event_logs.py --types ApiEvent,LoginEvent

Requires .env with Salesforce credentials (see sf_connect.py).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = DATA_DIR / "sf_event_logs"
MANIFEST_PATH = LOGS_DIR / "manifest.json"

from scripts.sf_connect import get_connection  # noqa: E402

try:
    from simple_salesforce import Salesforce
    from simple_salesforce.exceptions import SalesforceError
except ImportError:
    print("Error: simple-salesforce is not installed.")
    sys.exit(1)

import requests  # noqa: E402


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip()
    return (name or "unnamed")[:200]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _available_event_log_fields(sf: Salesforce) -> set[str]:
    """Return the set of field names actually present on EventLogFile in this org.

    ``Sequence`` and ``Interval`` only exist for orgs with **hourly** Event
    Log Files enabled. Without them, including those columns in a SOQL SELECT
    fails with INVALID_FIELD — so we probe the schema first.
    """
    describe = sf.restful("sobjects/EventLogFile/describe", method="GET") or {}
    return {f.get("name") for f in describe.get("fields", []) if f.get("name")}


def query_event_log_files(
    sf: Salesforce,
    *,
    days: int,
    event_types: list[str] | None,
) -> list[dict]:
    """Return EventLogFile rows from the last ``days`` days (capped at 365)."""
    # Salesforce accepts LAST_N_DAYS:<n> as a SOQL date literal up to 365.
    days = max(1, min(days, 365))
    where_clauses = [f"LogDate = LAST_N_DAYS:{days}"]
    if event_types:
        quoted = ",".join(f"'{t}'" for t in event_types)
        where_clauses.append(f"EventType IN ({quoted})")

    # Always-present fields per the standard EventLogFile schema.
    base_fields = [
        "Id", "EventType", "LogDate", "LogFileLength",
        "LogFileFieldNames", "ApiVersion", "CreatedDate",
    ]
    # Hourly-ELF-only fields — include only if the org's schema exposes them.
    available = _available_event_log_fields(sf)
    optional = [f for f in ("Sequence", "Interval") if f in available]
    select_fields = base_fields + optional

    order_by = "LogDate DESC, EventType"
    if "Sequence" in optional:
        order_by += ", Sequence"

    soql = (
        f"SELECT {', '.join(select_fields)} "
        f"FROM EventLogFile WHERE {' AND '.join(where_clauses)} "
        f"ORDER BY {order_by}"
    )
    result = sf.query_all(soql)
    return result.get("records", [])


def download_one(
    sf: Salesforce,
    session: requests.Session,
    record: dict,
    *,
    dry_run: bool,
    resume: bool,
    manifest: dict,
) -> str:
    """Download one EventLogFile.LogFile. Returns status: downloaded/skipped/error."""
    elf_id = record["Id"]
    event_type = record.get("EventType", "UnknownEvent")
    log_date = (record.get("LogDate") or "")[:10] or "unknown-date"
    interval = record.get("Interval") or "Daily"
    # Sequence is only set for hourly ELFs; fall back to Id suffix so multiple
    # daily rows of the same EventType+LogDate cannot collide on disk.
    seq = record.get("Sequence")
    discriminator = seq if seq not in (None, "") else elf_id

    subdir = LOGS_DIR / log_date
    filename = safe_filename(f"{event_type}_{interval}_{discriminator}.csv")
    filepath = subdir / filename

    if resume and filepath.exists() and filepath.stat().st_size > 0:
        return "skipped"
    if dry_run:
        return "would-download"

    url = f"{sf.base_url}sobjects/EventLogFile/{elf_id}/LogFile"
    try:
        resp = session.get(url, timeout=300)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        # 404 typically means retention expired between enumerate + fetch —
        # log but don't crash the run.
        print(f"    ! {event_type} {log_date}: {exc}")
        return "error"
    except requests.RequestException as exc:
        print(f"    ! {event_type} {log_date}: {exc}")
        return "error"

    subdir.mkdir(parents=True, exist_ok=True)
    filepath.write_bytes(resp.content)

    manifest[elf_id] = {
        "event_type": event_type,
        "log_date": log_date,
        "interval": interval,
        "sequence": seq,
        "size": len(resp.content),
        "path": str(filepath.relative_to(LOGS_DIR)),
        "field_names": record.get("LogFileFieldNames"),
    }
    return "downloaded"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=365,
                        help="How many days back to fetch (1–365, default 365).")
    parser.add_argument("--types", default=None,
                        help="Comma-separated EventType filter (e.g. ApiEvent,LoginEvent).")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be downloaded.")
    parser.add_argument("--resume", action="store_true",
                        help="Skip files already on disk.")
    args = parser.parse_args(argv)

    event_types = (
        [t.strip() for t in args.types.split(",") if t.strip()]
        if args.types else None
    )

    print("=" * 60)
    print("Salesforce Event Log File Download")
    print("=" * 60)

    print("\nConnecting to Salesforce...")
    try:
        sf = get_connection()
    except SystemExit:
        raise
    print(f"Connected to: {sf.sf_instance}")

    print(f"\nQuerying EventLogFile (last {args.days} days)...")
    try:
        records = query_event_log_files(sf, days=args.days, event_types=event_types)
    except SalesforceError as exc:
        msg = str(exc)
        print(f"\n  ! Could not query EventLogFile: {exc}")
        if "INVALID_TYPE" in msg or "sObject type 'EventLogFile'" in msg:
            print("  The EventLogFile object is not exposed in this org — that")
            print("  means no Event Monitoring licence at all. Without the")
            print("  licence retention is 1 day, so older logs are gone.")
        elif "INVALID_FIELD" in msg:
            print("  A field in the SELECT is not in this org's schema. We probe")
            print("  EventLogFile.describe to omit hourly-only fields, so this")
            print("  most likely means the org's schema lacks one of the base")
            print("  fields — escalate to a Salesforce admin.")
        else:
            print("  Without an Event Monitoring licence, retention is only 1 day,")
            print("  so older logs are permanently gone regardless of tooling.")
        return 2

    print(f"Found {len(records)} EventLogFile row(s)")
    if not records:
        print("\n  NOTE: no EventLogFile rows returned.")
        print("  Likely cause: Event Monitoring licence not enabled, so the")
        print("  retention is only 1 day and the log files have already expired.")
        print("  See: https://help.salesforce.com/s/articleView?id=sf.event_monitoring.htm")
        # Still write an empty manifest so downstream audit scripts know the
        # run happened.
        if not args.dry_run:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            MANIFEST_PATH.write_text(json.dumps({
                "queried_at": utc_now_iso(),
                "days": args.days,
                "records": 0,
                "note": "No EventLogFile rows — licence or retention limit.",
                "entries": {},
            }, indent=2))
        return 0

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing manifest for resume.
    manifest_entries: dict[str, Any] = {}
    if args.resume and MANIFEST_PATH.exists():
        try:
            prior = json.loads(MANIFEST_PATH.read_text())
            manifest_entries = prior.get("entries", {}) if isinstance(prior, dict) else {}
        except json.JSONDecodeError:
            pass

    # Authenticated binary download session — same pattern as download_files.py.
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {sf.session_id}",
        "Accept": "text/csv",
    })

    stats = {"downloaded": 0, "skipped": 0, "errors": 0, "would-download": 0}
    started = time.time()
    for i, rec in enumerate(records, start=1):
        status = download_one(
            sf, session, rec,
            dry_run=args.dry_run, resume=args.resume, manifest=manifest_entries,
        )
        stats[status] = stats.get(status, 0) + 1
        if i % 25 == 0:
            print(f"  [{i}/{len(records)}] progress: {stats}")

    # Persist manifest.
    if not args.dry_run:
        MANIFEST_PATH.write_text(json.dumps({
            "queried_at": utc_now_iso(),
            "days": args.days,
            "records": len(records),
            "entries": manifest_entries,
        }, indent=2, ensure_ascii=False, default=str))

    duration = round(time.time() - started, 2)
    print("\n" + "=" * 60)
    print(f"Done in {duration}s")
    for k, v in stats.items():
        if v:
            print(f"  {k}: {v}")
    print(f"  Output: {LOGS_DIR}")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
