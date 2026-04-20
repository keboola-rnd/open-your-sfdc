#!/usr/bin/env python3
"""Audit Salesforce file downloads - compare DB vs disk vs external references.

Reports:
  - DB record counts for every file-bearing object
  - Disk file counts and sizes
  - Manifest coverage (what's downloaded)
  - External references (ContentVersions referenced by FeedAttachment,
    ContentAsset, or EmailMessage that aren't in our ContentVersion export)

Run:
    python scripts/audit_files.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"

DB_PATH = DATA_DIR / "salesforce_full.db"
FILES_DIR = DATA_DIR / "sf_files"
MANIFEST_PATH = FILES_DIR / "manifest.json"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n = n / 1024
    return f"{n:.1f} TB"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    if not _table_exists(conn, table):
        return -1
    try:
        sql = f'SELECT COUNT(*) FROM "{table}"'
        if where:
            sql += f" WHERE {where}"
        return conn.execute(sql).fetchone()[0]
    except sqlite3.DatabaseError:
        return -1


def _disk_audit() -> dict[str, dict]:
    """Return file counts + sizes per top-level folder under sf_files/."""
    result: dict[str, dict] = {}
    if not FILES_DIR.exists():
        return result
    for sub in ("ContentVersion", "Attachment", "Document"):
        root = FILES_DIR / sub
        if not root.exists():
            result[sub] = {"files": 0, "bytes": 0}
            continue
        files = 0
        total_bytes = 0
        for p in root.rglob("*"):
            if p.is_file():
                files += 1
                total_bytes += p.stat().st_size
        result[sub] = {"files": files, "bytes": total_bytes}
    return result


def _manifest_by_type(manifest: dict) -> Counter:
    return Counter(v.get("type") for v in manifest.values())


def _missing_on_disk(manifest: dict) -> list[dict]:
    missing = []
    for key, entry in manifest.items():
        rel = entry.get("path")
        if not rel:
            continue
        p = FILES_DIR / rel
        if not p.exists() or p.stat().st_size == 0:
            missing.append({"id": key, **entry})
    return missing


def _filename_collisions(manifest: dict) -> list[tuple[str, list[dict]]]:
    """Find manifest paths that are shared by multiple entries (latest wins on disk)."""
    by_path: dict[str, list[dict]] = {}
    for key, entry in manifest.items():
        rel = entry.get("path")
        if not rel:
            continue
        by_path.setdefault(rel, []).append({"id": key, **entry})
    return [(p, entries) for p, entries in by_path.items() if len(entries) > 1]


def _external_cv_refs(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Find ContentVersion / ContentDocument IDs referenced by OTHER tables
    that aren't themselves in our ContentVersion or ContentDocument tables.

    These are candidates for "chatter attachments we didn't download".
    """
    cv_ids = {r[0] for r in conn.execute('SELECT "Id" FROM "ContentVersion"').fetchall()}
    cd_ids = {r[0] for r in conn.execute('SELECT "Id" FROM "ContentDocument"').fetchall()}

    refs: dict[str, list[str]] = {
        "FeedAttachment.RecordId (Type='Content')": [],
        "ContentAsset.ContentDocumentId": [],
        "EmailMessage with attachment": [],
        "SDOC__SDoc__c.SDOC__File_ID__c (generated PDFs)": [],
    }

    # FeedAttachment Content
    if _table_exists(conn, "FeedAttachment"):
        rows = conn.execute(
            "SELECT DISTINCT RecordId FROM FeedAttachment "
            "WHERE Type='Content' AND RecordId IS NOT NULL AND RecordId != ''"
        ).fetchall()
        refs["FeedAttachment.RecordId (Type='Content')"] = [
            r[0] for r in rows if r[0] not in cv_ids and r[0] not in cd_ids
        ]

    # ContentAsset
    if _table_exists(conn, "ContentAsset"):
        rows = conn.execute(
            "SELECT DISTINCT ContentDocumentId FROM ContentAsset "
            "WHERE ContentDocumentId IS NOT NULL AND ContentDocumentId != ''"
        ).fetchall()
        refs["ContentAsset.ContentDocumentId"] = [
            r[0] for r in rows if r[0] not in cd_ids
        ]

    # SDocs generated PDFs — each SDOC__SDoc__c row has a File_ID__c
    # pointing to a ContentVersion with the rendered PDF (typical invoices,
    # quotes, contracts). These are rarely included in a plain ContentVersion
    # query because of sharing rules.
    if _table_exists(conn, "SDOC__SDoc__c"):
        try:
            rows = conn.execute(
                'SELECT DISTINCT SDOC__File_ID__c FROM "SDOC__SDoc__c" '
                "WHERE SDOC__File_ID__c IS NOT NULL AND SDOC__File_ID__c != ''"
            ).fetchall()
            refs["SDOC__SDoc__c.SDOC__File_ID__c (generated PDFs)"] = [
                r[0] for r in rows if r[0] not in cv_ids
            ]
        except sqlite3.DatabaseError:
            pass

    # EmailMessage with attachment - these live in ContentDocumentLink as
    # LinkedEntityId = EmailMessageId. Count unresolved ContentDocuments.
    if _table_exists(conn, "ContentDocumentLink") and _table_exists(conn, "EmailMessage"):
        rows = conn.execute(
            "SELECT DISTINCT ContentDocumentId FROM ContentDocumentLink "
            "WHERE LinkedEntityId IN (SELECT Id FROM EmailMessage) "
        ).fetchall()
        refs["EmailMessage with attachment"] = [
            r[0] for r in rows if r[0] not in cd_ids
        ]

    return refs


def main() -> int:
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print("SALESFORCE FILE DOWNLOAD AUDIT")
    print("=" * 70)

    # 1. DB file-bearing tables
    print("\n## Database (file-bearing objects)")
    file_objects = [
        ("ContentVersion", "IsLatest=1"),
        ("ContentVersion", ""),
        ("ContentDocument", ""),
        ("ContentDocumentLink", ""),
        ("Attachment", ""),
        ("Document", ""),
        ("Note", ""),
        ("ContentNote", ""),
        ("ContentAsset", ""),
        ("FeedItem", "Type='ContentPost'"),
        ("FeedAttachment", ""),
        ("FeedAttachment", "Type='Content'"),
    ]
    for table, where in file_objects:
        count = _count(conn, table, where)
        if count < 0:
            label = f"{table} (not in export)"
            print(f"  - {label}")
            continue
        label = f"{table}" + (f" WHERE {where}" if where else "")
        size_hint = ""
        # Bytes hint
        if table == "ContentVersion":
            try:
                b = conn.execute(
                    'SELECT SUM(ContentSize) FROM "ContentVersion"'
                    + (f" WHERE {where}" if where else "")
                ).fetchone()[0] or 0
                size_hint = f"  ({_fmt_bytes(b)} total)"
            except sqlite3.DatabaseError:
                pass
        elif table == "Attachment":
            try:
                b = conn.execute('SELECT SUM(BodyLength) FROM "Attachment"').fetchone()[0] or 0
                size_hint = f"  ({_fmt_bytes(b)} total)"
            except sqlite3.DatabaseError:
                pass
        elif table == "Document":
            try:
                b = conn.execute('SELECT SUM(BodyLength) FROM "Document"').fetchone()[0] or 0
                size_hint = f"  ({_fmt_bytes(b)} total)"
            except sqlite3.DatabaseError:
                pass
        print(f"  - {label}: {count:,}{size_hint}")

    # 2. Disk audit
    print("\n## Downloaded to disk (data/sf_files)")
    disk = _disk_audit()
    total_disk_bytes = 0
    total_disk_files = 0
    for sub, info in disk.items():
        print(f"  - {sub}/: {info['files']:,} files, {_fmt_bytes(info['bytes'])}")
        total_disk_bytes += info["bytes"]
        total_disk_files += info["files"]
    print(f"  TOTAL: {total_disk_files:,} files, {_fmt_bytes(total_disk_bytes)}")

    # 3. Manifest
    print("\n## Manifest (data/sf_files/manifest.json)")
    if not MANIFEST_PATH.exists():
        print("  (missing - run `make files` first)")
        return 1
    manifest = json.loads(MANIFEST_PATH.read_text())
    by_type = _manifest_by_type(manifest)
    print(f"  Total entries: {len(manifest):,}")
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"    {t}: {c:,}")

    missing_disk = _missing_on_disk(manifest)
    if missing_disk:
        print(f"  ⚠  Listed in manifest but missing/empty on disk: {len(missing_disk)}")
        for m in missing_disk[:5]:
            print(f"      {m['type']} {m['id']}: {m.get('name') or m.get('title')}")

    collisions = _filename_collisions(manifest)
    if collisions:
        lost = sum(len(entries) - 1 for _, entries in collisions)
        print(f"  ⚠  Filename collisions: {len(collisions)} path(s) shared by "
              f"{lost + len(collisions)} entries → {lost} file(s) overwritten on disk")
        for p, entries in collisions[:3]:
            print(f"      {p}")
            for e in entries:
                print(f"        - {e['type']} {e['id']}: {e.get('name') or e.get('title')}")

    # 4. Coverage - manifest vs DB
    print("\n## DB → Manifest coverage")
    cv_db = {r[0] for r in conn.execute('SELECT "Id" FROM "ContentVersion"').fetchall()}
    att_db = {r[0] for r in conn.execute('SELECT "Id" FROM "Attachment"').fetchall()}
    doc_db = {r[0] for r in conn.execute('SELECT "Id" FROM "Document"').fetchall()}

    cv_mf = {k for k, v in manifest.items() if v.get("type") == "ContentVersion"}
    att_mf = {k for k, v in manifest.items() if v.get("type") == "Attachment"}
    doc_mf = {k for k, v in manifest.items() if v.get("type") == "Document"}

    print(f"  ContentVersion   {len(cv_db & cv_mf):>4} / {len(cv_db):>4}"
          f"  {'✓' if cv_db == cv_mf else '⚠ ' + str(len(cv_db - cv_mf)) + ' missing'}")
    print(f"  Attachment       {len(att_db & att_mf):>4} / {len(att_db):>4}"
          f"  {'✓' if att_db == att_mf else '⚠ ' + str(len(att_db - att_mf)) + ' missing'}")
    print(f"  Document         {len(doc_db & doc_mf):>4} / {len(doc_db):>4}"
          f"  {'✓' if doc_db == doc_mf else '⚠ ' + str(len(doc_db - doc_mf)) + ' missing'}")

    # 5. External references (what's linked from elsewhere but not in our CV/CD tables)
    print("\n## External references to files NOT in our export")
    print("  (ContentVersion/Document IDs referenced from other objects")
    print("   that aren't themselves exported — these binaries won't be downloaded)")
    refs = _external_cv_refs(conn)
    total_missing = 0
    for src, ids in refs.items():
        marker = "⚠" if ids else "✓"
        print(f"  {marker} {src}: {len(ids)}")
        total_missing += len(ids)
        # Show a few examples
        for rid in ids[:3]:
            print(f"      {rid}")

    # 6. Final verdict
    print("\n" + "=" * 70)
    gaps = bool(total_missing or missing_disk or collisions)
    if not gaps:
        print("VERDICT: ✓  All downloadable binaries are on disk.")
    else:
        print("VERDICT: ⚠  Gaps detected.")
        if total_missing:
            print(f"  - {total_missing} file binaries referenced from other objects")
            print(f"    are not in our ContentVersion/ContentDocument tables.")
            print(f"    Run `make files-gaps` to fetch them directly.")
        if missing_disk:
            print(f"  - {len(missing_disk)} manifest entries have no file on disk.")
            print(f"    Run `python scripts/download_files.py --resume` to retry.")
        if collisions:
            lost = sum(len(e) - 1 for _, e in collisions)
            print(f"  - {lost} file(s) lost to filename collisions (same name on")
            print(f"    same parent overwrites previous). Re-run `make files`")
            print(f"    — the updated script prefixes with the record Id to avoid this.")
    print("=" * 70)
    return 0 if not gaps else 2


if __name__ == "__main__":
    sys.exit(main())
