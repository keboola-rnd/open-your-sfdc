#!/usr/bin/env python3
"""Audit the complete Salesforce archive — what ran, what's missing, what's next.

Checks every output directory produced by ``make archive`` and prints a
compact report. Use as the final step of a backup run or periodically to
confirm the archive is still whole.

Covers:
  1. Data export (data/sf_full_export + _export_log.json)
  2. Imported DB (data/salesforce_full.db)
  3. Binary files (delegates to audit_files.py)
  4. Metadata via Tooling API (data/sf_metadata/_index.json)
  5. SFDX metadata retrieve (data/sf_sfdx_metadata/)
  6. Event Monitoring logs (data/sf_event_logs/manifest.json)
  7. Licence hints (Shield / Event Monitoring presence in DB)

Usage:
    python scripts/audit_archive.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"
EXPORT_DIR = DATA_DIR / "sf_full_export"
EXPORT_LOG = EXPORT_DIR / "_export_log.json"
EXPORT_METADATA = EXPORT_DIR / "metadata.json"
DB_PATH = DATA_DIR / "salesforce_full.db"
FILES_DIR = DATA_DIR / "sf_files"
METADATA_DIR = DATA_DIR / "sf_metadata"
METADATA_INDEX = METADATA_DIR / "_index.json"
SFDX_DIR = DATA_DIR / "sf_sfdx_metadata"
EVENT_LOGS_DIR = DATA_DIR / "sf_event_logs"
EVENT_LOGS_MANIFEST = EVENT_LOGS_DIR / "manifest.json"


def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024:
            return f"{x:.1f} {unit}"
        x = x / 1024
    return f"{x:.1f} TB"


def _age(iso: str | None) -> str:
    """Turn an ISO timestamp into 'N hours / days ago'."""
    if not iso:
        return "unknown"
    try:
        t = datetime.strptime(iso.rstrip("Z"), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - t
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)} min ago"
    if hours < 48:
        return f"{hours:.1f} hours ago"
    return f"{delta.days} days ago"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _count_tree(root: Path, pattern: str = "*") -> tuple[int, int]:
    """Return (file_count, byte_total) for every file under ``root``."""
    if not root.exists():
        return 0, 0
    files = 0
    total = 0
    for p in root.rglob(pattern):
        if p.is_file():
            files += 1
            total += p.stat().st_size
    return files, total


# -----------------------------------------------------------------------------

def audit_data_export(findings: list[str]) -> None:
    print("\n## 1. Data export  (make export)")
    if not EXPORT_LOG.exists():
        print("  ✗ no _export_log.json — never ran `make export`")
        findings.append("Run `make export` to dump every SObject to CSV.")
        return
    log = json.loads(EXPORT_LOG.read_text())
    started = log.get("started_at")
    finished = log.get("finished_at")
    total_objects = log.get("total_objects", 0)
    total_records = log.get("total_records", 0)
    errors = log.get("errors", [])
    print(f"  ✓ last run: {finished}  ({_age(finished)})")
    print(f"    objects: {total_objects:,}   records: {total_records:,}")
    if errors:
        print(f"    ⚠ errors: {len(errors)}")
        for e in errors[:5]:
            print(f"      - {e}")
        findings.append(f"{len(errors)} error(s) in last export — investigate _export_log.json.")

    # Completeness signal: are SetupAuditTrail and core metadata-ish objects present?
    if EXPORT_METADATA.exists():
        meta = json.loads(EXPORT_METADATA.read_text())
        objs = meta.get("objects", {})
        flagged = {
            "SetupAuditTrail": "180-day Setup change history",
            "ApexClass": "Apex source code",
            "AuraDefinitionBundle": "Aura components",
        }
        missing = [name for name in flagged if name not in objs]
        if missing:
            for m in missing:
                print(f"    ⚠ missing from export: {m} ({flagged[m]})")
                findings.append(f"{m} not exported — check SKIP_OBJECTS / permissions.")


def audit_imported_db(findings: list[str]) -> None:
    print("\n## 2. Imported SQLite DB  (make import)")
    if not DB_PATH.exists():
        print(f"  ✗ {DB_PATH} missing — run `make import`")
        findings.append("Run `make import` to build salesforce_full.db from CSV.")
        return
    mtime = datetime.fromtimestamp(DB_PATH.stat().st_mtime, tz=timezone.utc)
    size = DB_PATH.stat().st_size
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE '\\_fts\\_%' ESCAPE '\\'"
        ).fetchone()[0]
        total_rows = 0
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_fts\\_%' ESCAPE '\\'"
        ):
            try:
                total_rows += conn.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0]
            except sqlite3.DatabaseError:
                continue
    finally:
        conn.close()
    print(f"  ✓ {DB_PATH.name}: {_fmt_bytes(size)}, {tables} tables, {total_rows:,} rows")
    print(f"    mtime: {mtime.strftime('%Y-%m-%dT%H:%M:%SZ')}  ({_age(mtime.strftime('%Y-%m-%dT%H:%M:%SZ'))})")


def audit_binary_files(findings: list[str]) -> None:
    print("\n## 3. Binary files  (make files / files-gaps)")
    if not FILES_DIR.exists():
        print("  ✗ data/sf_files missing — run `make files`")
        findings.append("Run `make files` to download Attachments / ContentVersions.")
        return
    files, total = _count_tree(FILES_DIR)
    print(f"  ✓ {files:,} file(s), {_fmt_bytes(total)} on disk")
    # Run audit_files.py in-process for the coverage details.
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT_DIR / "scripts" / "audit_files.py")],
            capture_output=True, text=True, check=False, timeout=60,
        )
        if "Gaps detected" in r.stdout:
            findings.append("`make files-audit` reports gaps — see its output for details.")
        # Surface just the last verdict line from audit_files output.
        for line in r.stdout.splitlines()[-10:]:
            stripped = line.strip()
            if stripped.startswith("VERDICT"):
                print(f"    delegate → {stripped}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def audit_metadata(findings: list[str]) -> None:
    print("\n## 4. Metadata via Tooling API  (make metadata)")
    if not METADATA_INDEX.exists():
        print(f"  ✗ {METADATA_INDEX} missing — run `make metadata`")
        findings.append("Run `make metadata` to grab Flows / ValidationRules / LWC / Workflow.")
        return
    idx = json.loads(METADATA_INDEX.read_text())
    finished = idx.get("finished_at")
    print(f"  ✓ last run: {finished}  ({_age(finished)})")
    for t in idx.get("types", []):
        total = t.get("total", 0)
        written = t.get("written", 0)
        errs = t.get("errors", 0)
        badge = "✓" if errs == 0 else "⚠"
        print(f"    {badge} {t['type']:<32} total={total:>5}  written={written:>5}  errors={errs}")
        if errs:
            findings.append(
                f"{errs} error(s) in metadata type {t['type']} — check _index.json for details."
            )
    # Concrete file counts for the user's favourites.
    for subdir_name, label in (
        ("Flow", "Flow versions"),
        ("ValidationRule", "Validation rules"),
        ("WorkflowRule", "Workflow rules"),
        ("LightningComponentBundle", "LWC bundles"),
        ("CustomField", "Custom field metadata records"),
    ):
        sub = METADATA_DIR / subdir_name
        if sub.exists():
            count, _ = _count_tree(sub, "*.json")
            print(f"    → {label}: {count:,} file(s) on disk")


def audit_sfdx(findings: list[str]) -> None:
    print("\n## 5. SFDX metadata retrieve  (make sfdx-retrieve)")
    if not SFDX_DIR.exists():
        print("  ✗ data/sf_sfdx_metadata missing — run `make sfdx-retrieve`")
        findings.append(
            "Run `make sfdx-retrieve` for Layouts / Profiles / CustomLabels XML."
        )
        return
    files, total = _count_tree(SFDX_DIR)
    if files == 0:
        print("  ⚠ directory exists but is empty — retrieve probably failed")
        findings.append("`make sfdx-retrieve` output is empty; check alias and `sf` CLI auth.")
        return
    print(f"  ✓ {files:,} file(s), {_fmt_bytes(total)}")

    # Count files per metadata type folder (subdir under main/default/).
    type_root = SFDX_DIR / "main" / "default"
    if type_root.exists():
        type_counts = Counter()
        for p in type_root.iterdir():
            if p.is_dir():
                sub_files, _ = _count_tree(p)
                type_counts[p.name] = sub_files
        top = type_counts.most_common(10)
        print(f"    top types: "
              + ", ".join(f"{t}={n}" for t, n in top))


def audit_event_logs(findings: list[str]) -> None:
    print("\n## 6. Event Monitoring logs  (make event-logs)")
    if not EVENT_LOGS_MANIFEST.exists():
        print(f"  - {EVENT_LOGS_MANIFEST} missing (may be expected — needs licence)")
        findings.append(
            "If you have an Event Monitoring licence, run `make event-logs` "
            "(1-day retention without the licence makes this time-sensitive)."
        )
        return
    mf = json.loads(EVENT_LOGS_MANIFEST.read_text())
    queried_at = mf.get("queried_at")
    entries = mf.get("entries", {})
    days = mf.get("days", "?")
    records = mf.get("records", 0)
    if not entries:
        print(f"  - empty manifest ({_age(queried_at)}): {mf.get('note', 'no log files')}")
        return
    total_bytes = sum(e.get("size", 0) for e in entries.values())
    by_type: Counter = Counter(e.get("event_type") for e in entries.values())
    print(f"  ✓ {len(entries):,} log files, {_fmt_bytes(total_bytes)} ({records:,} records in query, {days} days)")
    print(f"    last run: {queried_at}  ({_age(queried_at)})")
    for t, c in by_type.most_common(10):
        print(f"      {t}: {c}")


def audit_licences_hint(findings: list[str]) -> None:
    print("\n## 7. Licence hints (from DB where possible)")
    if not DB_PATH.exists():
        print("  - DB not present, skipping")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        # Shield Platform Encryption is visible through TenantSecret (queryable
        # only if the org has the licence). Some exports may have empty rows.
        if _table_exists(conn, "TenantSecret"):
            n = conn.execute('SELECT COUNT(*) FROM "TenantSecret"').fetchone()[0]
            if n:
                print(f"  ⚠ Shield Platform Encryption: {n} TenantSecret rows — "
                      f"any Shield-encrypted field values should be decrypted "
                      f"before the org is shut down, otherwise they remain encrypted.")
                findings.append(
                    "Shield in use — identify Shield-encrypted fields (Setup → "
                    "Encryption Policy) and export decrypted copies now."
                )
            else:
                print("  ✓ No TenantSecret rows — Shield likely not in use.")
        else:
            print("  ? TenantSecret not in DB — Shield check inconclusive.")

        if _table_exists(conn, "EventLogFile"):
            n = conn.execute('SELECT COUNT(*) FROM "EventLogFile"').fetchone()[0]
            if n:
                print(f"  ✓ Event Monitoring: EventLogFile has {n} rows — licence likely present.")
            else:
                print("  ⚠ EventLogFile table exists but empty — probably no licence or no recent activity.")
        else:
            print("  - EventLogFile not in export (blocked by SKIP_OBJECTS, which is expected).")
    finally:
        conn.close()


def main() -> int:
    print("=" * 70)
    print("OPEN-YOUR-SFDC  —  ARCHIVE AUDIT")
    print("=" * 70)

    findings: list[str] = []
    audit_data_export(findings)
    audit_imported_db(findings)
    audit_binary_files(findings)
    audit_metadata(findings)
    audit_sfdx(findings)
    audit_event_logs(findings)
    audit_licences_hint(findings)

    print("\n" + "=" * 70)
    if not findings:
        print("VERDICT: ✓  Archive looks complete.")
    else:
        print(f"VERDICT: ⚠  {len(findings)} action item(s):")
        for f in findings:
            print(f"  - {f}")
    print("=" * 70)
    return 0 if not findings else 2


if __name__ == "__main__":
    sys.exit(main())
