#!/usr/bin/env python3
"""
Download binary files from Salesforce before org shutdown.

Downloads:
  1. ContentVersion files (Lightning files) - the actual binary via VersionData
  2. Attachment files (classic attachments) - the actual binary via Body
  3. Document files (classic documents) - the actual binary via Body

Each file is saved with its original name into a structured directory:
  data/sf_files/
    ContentVersion/{ContentDocumentId}/{Title}.{ext}
    Attachment/{ParentId}/{Name}
    Document/{FolderId}/{Name}

Also exports a manifest.json mapping IDs to file paths, parent records, etc.

Usage:
    python scripts/download_files.py                     # Download all
    python scripts/download_files.py --type attachment    # Only attachments
    python scripts/download_files.py --type content       # Only ContentVersion
    python scripts/download_files.py --type document      # Only classic documents
    python scripts/download_files.py --resume             # Skip already downloaded
    python scripts/download_files.py --dry-run            # Show what would be downloaded

Requires .env with Salesforce credentials.
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

from scripts.sf_connect import get_connection  # noqa: E402

try:
    from simple_salesforce import Salesforce
except ImportError:
    print("Error: simple-salesforce is not installed.")
    sys.exit(1)

import requests  # noqa: E402

FILES_DIR = DATA_DIR / "sf_files"
MANIFEST_PATH = FILES_DIR / "manifest.json"


def safe_filename(name: str) -> str:
    """Remove or replace characters that aren't safe for filenames."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    return name[:200]  # Cap length


def download_content_versions(
    sf: Salesforce,
    session: requests.Session,
    *,
    resume: bool = False,
    dry_run: bool = False,
    manifest: dict,
) -> dict[str, Any]:
    """Download ContentVersion files (Lightning file system)."""
    print("\n=== ContentVersion (Lightning Files) ===")

    # Query all latest versions.
    soql = (
        "SELECT Id, ContentDocumentId, Title, FileExtension, ContentSize, "
        "PathOnClient, VersionNumber, CreatedDate, CreatedById, "
        "Description, IsLatest "
        "FROM ContentVersion WHERE IsLatest = true"
    )
    results = sf.query_all(soql)
    records = results.get("records", [])
    print(f"Found {len(records)} files")

    stats = {"total": len(records), "downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0}
    out_dir = FILES_DIR / "ContentVersion"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, rec in enumerate(records):
        cv_id = rec["Id"]
        doc_id = rec.get("ContentDocumentId", "unknown")
        title = rec.get("Title", cv_id)
        ext = rec.get("FileExtension", "")
        size = rec.get("ContentSize", 0)

        filename = safe_filename(f"{title}.{ext}" if ext else title)
        subdir = out_dir / safe_filename(doc_id)
        filepath = subdir / filename

        if resume and filepath.exists() and filepath.stat().st_size > 0:
            stats["skipped"] += 1
            continue

        if dry_run:
            print(f"  [{i + 1}/{len(records)}] Would download: {title}.{ext} ({size:,} bytes)")
            stats["downloaded"] += 1
            continue

        # Download binary via REST API.
        url = f"{sf.base_url}sobjects/ContentVersion/{cv_id}/VersionData"
        try:
            resp = session.get(url, timeout=120)
            resp.raise_for_status()

            subdir.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(resp.content)
            stats["downloaded"] += 1
            stats["bytes"] += len(resp.content)

            manifest[cv_id] = {
                "type": "ContentVersion",
                "content_document_id": doc_id,
                "title": title,
                "extension": ext,
                "size": size,
                "path": str(filepath.relative_to(FILES_DIR)),
                "created_date": rec.get("CreatedDate"),
            }

            if (i + 1) % 10 == 0:
                print(f"  [{i + 1}/{len(records)}] Downloaded {title}.{ext} ({size:,} bytes)")

        except Exception as exc:
            stats["errors"] += 1
            print(f"  ! [{i + 1}] {title}: {exc}")

    return stats


def download_attachments(
    sf: Salesforce,
    session: requests.Session,
    *,
    resume: bool = False,
    dry_run: bool = False,
    manifest: dict,
) -> dict[str, Any]:
    """Download classic Attachment files."""
    print("\n=== Attachments (Classic) ===")

    soql = (
        "SELECT Id, ParentId, Name, ContentType, BodyLength, "
        "CreatedDate, CreatedById, Description "
        "FROM Attachment"
    )
    results = sf.query_all(soql)
    records = results.get("records", [])
    print(f"Found {len(records)} attachments")

    stats = {"total": len(records), "downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0}
    out_dir = FILES_DIR / "Attachment"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, rec in enumerate(records):
        att_id = rec["Id"]
        parent_id = rec.get("ParentId", "unknown")
        name = rec.get("Name", att_id)
        size = rec.get("BodyLength", 0)

        filename = safe_filename(name)
        subdir = out_dir / safe_filename(parent_id)
        # Collision guard: multiple attachments on the same parent can share a
        # filename. Prefix with the attachment Id short form to disambiguate.
        filepath = subdir / f"{att_id[:15]}__{filename}"

        if resume and filepath.exists() and filepath.stat().st_size > 0:
            stats["skipped"] += 1
            continue

        if dry_run:
            print(f"  [{i + 1}/{len(records)}] Would download: {name} ({size:,} bytes)")
            stats["downloaded"] += 1
            continue

        url = f"{sf.base_url}sobjects/Attachment/{att_id}/Body"
        try:
            resp = session.get(url, timeout=120)
            resp.raise_for_status()

            subdir.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(resp.content)
            stats["downloaded"] += 1
            stats["bytes"] += len(resp.content)

            manifest[att_id] = {
                "type": "Attachment",
                "parent_id": parent_id,
                "name": name,
                "content_type": rec.get("ContentType"),
                "size": size,
                "path": str(filepath.relative_to(FILES_DIR)),
                "created_date": rec.get("CreatedDate"),
            }

            if (i + 1) % 10 == 0:
                print(f"  [{i + 1}/{len(records)}] Downloaded {name} ({size:,} bytes)")

        except Exception as exc:
            stats["errors"] += 1
            print(f"  ! [{i + 1}] {name}: {exc}")

    return stats


def download_external_refs(
    sf: Salesforce,
    session: requests.Session,
    *,
    resume: bool = False,
    dry_run: bool = False,
    manifest: dict,
) -> dict[str, Any]:
    """Download ContentVersion binaries referenced by FeedAttachment / ContentAsset.

    Many Chatter attachments point to ContentVersion IDs that are not returned
    by the plain ``SELECT * FROM ContentVersion`` query (permission scoping,
    community-only, …). This function reads such IDs from our local SQLite DB
    and fetches each binary directly by Id, bypassing the ContentVersion index.
    """
    print("\n=== External refs (Chatter / ContentAsset) ===")

    import sqlite3

    db_path = DATA_DIR / "salesforce_full.db"
    if not db_path.exists():
        print(f"  (DB not found at {db_path} — skipping)")
        return {"total": 0, "downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    cv_ids = {r[0] for r in conn.execute('SELECT "Id" FROM ContentVersion').fetchall()}
    cd_ids = {r[0] for r in conn.execute('SELECT "Id" FROM ContentDocument').fetchall()}

    # FeedAttachment Content → ContentVersion IDs
    fa_targets: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT RecordId FROM FeedAttachment "
            "WHERE Type='Content' AND RecordId IS NOT NULL AND RecordId != ''"
        ).fetchall()
        fa_targets = {r[0] for r in rows if r[0] not in cv_ids}
    except sqlite3.DatabaseError:
        pass

    # ContentAsset → ContentDocument IDs (we then need latest ContentVersion)
    ca_doc_targets: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT ContentDocumentId FROM ContentAsset "
            "WHERE ContentDocumentId IS NOT NULL AND ContentDocumentId != ''"
        ).fetchall()
        ca_doc_targets = {r[0] for r in rows if r[0] not in cd_ids}
    except sqlite3.DatabaseError:
        pass

    # SDocs generated PDFs (invoices, quotes, contracts) — each SDoc row has
    # a File_ID__c pointing to a ContentVersion. Thousands of historical
    # invoice PDFs typically live here.
    sdoc_targets: set[str] = set()
    sdoc_metadata: dict[str, dict] = {}
    try:
        rows = conn.execute(
            'SELECT Id, SDOC__File_ID__c AS cv_id, '
            'SDOC__Attachment_Name__c AS filename, '
            'SDOC__Document_Number__c AS doc_num, '
            'SDOC__ObjectID__c AS parent_id, '
            'SDOC__ObjectType__c AS parent_type '
            'FROM "SDOC__SDoc__c" '
            "WHERE SDOC__File_ID__c IS NOT NULL AND SDOC__File_ID__c != ''"
        ).fetchall()
        for r in rows:
            cv = r["cv_id"]
            if cv and cv not in cv_ids:
                sdoc_targets.add(cv)
                sdoc_metadata[cv] = {
                    "sdoc_id": r["Id"],
                    "filename": r["filename"] or f"{r['doc_num'] or cv}.pdf",
                    "parent_id": r["parent_id"],
                    "parent_type": r["parent_type"],
                }
    except sqlite3.DatabaseError:
        pass

    conn.close()

    total = len(fa_targets) + len(ca_doc_targets) + len(sdoc_targets)
    print(f"Found {len(fa_targets)} chatter ContentVersion refs, "
          f"{len(ca_doc_targets)} ContentAsset ContentDocument refs, "
          f"{len(sdoc_targets)} SDocs PDFs (invoices/quotes)")
    print(f"  Total external targets: {total}")

    stats = {"total": total, "downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0}

    if not total:
        return stats

    out_dir = FILES_DIR / "ContentVersion"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve ContentAsset docs → latest CV ids via SOQL.
    if ca_doc_targets:
        print(f"  Resolving {len(ca_doc_targets)} ContentDocuments to latest ContentVersions...")
        doc_ids = list(ca_doc_targets)
        for chunk_start in range(0, len(doc_ids), 200):
            chunk = doc_ids[chunk_start:chunk_start + 200]
            placeholders = ",".join(f"'{d}'" for d in chunk)
            soql = (
                f"SELECT Id, ContentDocumentId, Title, FileExtension, ContentSize, "
                f"CreatedDate FROM ContentVersion "
                f"WHERE ContentDocumentId IN ({placeholders}) AND IsLatest = true"
            )
            try:
                res = sf.query_all(soql)
                for rec in res.get("records", []):
                    fa_targets.add(rec["Id"])
                    # Stash record metadata so we can name the file properly.
                    manifest.setdefault(f"__ca_meta_{rec['Id']}", rec)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! ContentAsset lookup chunk failed: {exc}")

    # Merge all CV targets into a single download queue, carrying known metadata
    # so we can name SDocs PDFs properly (301000587.pdf instead of Id-only).
    all_targets = fa_targets | sdoc_targets
    for cv_id, md in sdoc_metadata.items():
        manifest.setdefault(f"__sdoc_meta_{cv_id}", md)

    target_list = sorted(all_targets)
    print(f"  Downloading {len(target_list)} external ContentVersions...")

    for i, cv_id in enumerate(target_list):
        meta = manifest.get(f"__ca_meta_{cv_id}") or {}
        sdoc_md = manifest.get(f"__sdoc_meta_{cv_id}") or {}
        title = meta.get("Title", cv_id)
        ext = meta.get("FileExtension", "")
        doc_id = meta.get("ContentDocumentId", "external")

        # SDocs: prefer the business-friendly filename (e.g. 301000587.pdf)
        if sdoc_md.get("filename"):
            title = sdoc_md["filename"]
            if not ext and "." in title:
                ext = title.rsplit(".", 1)[-1]
                title = title.rsplit(".", 1)[0]
            doc_id = sdoc_md.get("parent_id") or doc_id

        # Look up metadata if we don't have it (FeedAttachment path).
        if not ext and not sdoc_md:
            try:
                lookup = sf.query(
                    f"SELECT Id, ContentDocumentId, Title, FileExtension, "
                    f"ContentSize, CreatedDate FROM ContentVersion "
                    f"WHERE Id = '{cv_id}'"
                )
                recs = lookup.get("records", [])
                if recs:
                    meta = recs[0]
                    title = meta.get("Title", cv_id)
                    ext = meta.get("FileExtension", "")
                    doc_id = meta.get("ContentDocumentId", "external")
            except Exception:  # noqa: BLE001
                pass

        filename = safe_filename(f"{title}.{ext}" if ext else title)
        subdir = out_dir / safe_filename(doc_id)
        filepath = subdir / filename

        if resume and filepath.exists() and filepath.stat().st_size > 0:
            stats["skipped"] += 1
            continue
        if dry_run:
            print(f"  [{i + 1}/{total}] Would download: {title}.{ext}")
            stats["downloaded"] += 1
            continue

        url = f"{sf.base_url}sobjects/ContentVersion/{cv_id}/VersionData"
        try:
            resp = session.get(url, timeout=120)
            resp.raise_for_status()
            subdir.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(resp.content)
            stats["downloaded"] += 1
            stats["bytes"] += len(resp.content)

            manifest[cv_id] = {
                "type": "ContentVersion",
                "content_document_id": doc_id,
                "title": title,
                "extension": ext,
                "size": meta.get("ContentSize", len(resp.content)),
                "path": str(filepath.relative_to(FILES_DIR)),
                "created_date": meta.get("CreatedDate"),
                "source": "sdoc" if sdoc_md else "external_ref",
                **({"sdoc_id": sdoc_md["sdoc_id"],
                    "parent_id": sdoc_md.get("parent_id"),
                    "parent_type": sdoc_md.get("parent_type")} if sdoc_md else {}),
            }
            if (i + 1) % 10 == 0:
                print(f"  [{i + 1}/{total}] Downloaded {title}.{ext}")
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"  ! [{i + 1}] {cv_id}: {exc}")

    # Strip cache entries.
    for key in list(manifest.keys()):
        if key.startswith("__ca_meta_") or key.startswith("__sdoc_meta_"):
            del manifest[key]

    return stats


def download_documents(
    sf: Salesforce,
    session: requests.Session,
    *,
    resume: bool = False,
    dry_run: bool = False,
    manifest: dict,
) -> dict[str, Any]:
    """Download classic Document files."""
    print("\n=== Documents (Classic) ===")

    soql = (
        "SELECT Id, FolderId, Name, ContentType, BodyLength, Type, "
        "CreatedDate, CreatedById, Description, DeveloperName "
        "FROM Document"
    )
    results = sf.query_all(soql)
    records = results.get("records", [])
    print(f"Found {len(records)} documents")

    stats = {"total": len(records), "downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0}
    out_dir = FILES_DIR / "Document"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, rec in enumerate(records):
        doc_id = rec["Id"]
        folder_id = rec.get("FolderId", "unknown")
        name = rec.get("Name", doc_id)
        size = rec.get("BodyLength", 0)

        filename = safe_filename(name)
        subdir = out_dir / safe_filename(folder_id)
        filepath = subdir / filename

        if resume and filepath.exists() and filepath.stat().st_size > 0:
            stats["skipped"] += 1
            continue

        if dry_run:
            print(f"  [{i + 1}/{len(records)}] Would download: {name} ({size:,} bytes)")
            stats["downloaded"] += 1
            continue

        url = f"{sf.base_url}sobjects/Document/{doc_id}/Body"
        try:
            resp = session.get(url, timeout=120)
            resp.raise_for_status()

            subdir.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(resp.content)
            stats["downloaded"] += 1
            stats["bytes"] += len(resp.content)

            manifest[doc_id] = {
                "type": "Document",
                "folder_id": folder_id,
                "name": name,
                "content_type": rec.get("ContentType"),
                "size": size,
                "path": str(filepath.relative_to(FILES_DIR)),
                "created_date": rec.get("CreatedDate"),
            }

            if (i + 1) % 10 == 0:
                print(f"  [{i + 1}/{len(records)}] Downloaded {name} ({size:,} bytes)")

        except Exception as exc:
            stats["errors"] += 1
            print(f"  ! [{i + 1}] {name}: {exc}")

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type", choices=["all", "content", "attachment", "document", "external"],
                        default="all", help="Which file types to download (default: all)")
    parser.add_argument("--resume", action="store_true", help="Skip already downloaded files")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be downloaded")
    parser.add_argument("--include-external-refs", action="store_true",
                        help="Also fetch ContentVersions referenced from FeedAttachment / ContentAsset "
                             "that aren't in the main ContentVersion export")
    args = parser.parse_args()

    FILES_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing manifest for resume.
    manifest: dict[str, Any] = {}
    if args.resume and MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            manifest = json.load(f)
        print(f"Loaded manifest with {len(manifest)} entries")

    print("Connecting to Salesforce...")
    sf = get_connection()
    print(f"Connected to {sf.sf_instance}")

    # Create an authenticated requests session for binary downloads.
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {sf.session_id}",
        "Accept": "application/octet-stream",
    })

    all_stats: dict[str, dict] = {}

    if args.type in ("all", "content"):
        all_stats["ContentVersion"] = download_content_versions(
            sf, session, resume=args.resume, dry_run=args.dry_run, manifest=manifest,
        )

    if args.type in ("all", "attachment"):
        all_stats["Attachment"] = download_attachments(
            sf, session, resume=args.resume, dry_run=args.dry_run, manifest=manifest,
        )

    if args.type in ("all", "document"):
        all_stats["Document"] = download_documents(
            sf, session, resume=args.resume, dry_run=args.dry_run, manifest=manifest,
        )

    if args.type in ("all", "external") or args.include_external_refs:
        all_stats["ExternalRefs"] = download_external_refs(
            sf, session, resume=args.resume, dry_run=args.dry_run, manifest=manifest,
        )

    # Save manifest.
    if not args.dry_run:
        with open(MANIFEST_PATH, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"\nManifest saved: {MANIFEST_PATH}")

    # Summary.
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total_bytes = 0
    for file_type, stats in all_stats.items():
        print(f"  {file_type}: {stats['downloaded']} downloaded, "
              f"{stats['skipped']} skipped, {stats['errors']} errors "
              f"(of {stats['total']} total)")
        total_bytes += stats.get("bytes", 0)
    print(f"\n  Total downloaded: {total_bytes / 1024 / 1024:.1f} MB")
    print(f"  Output: {FILES_DIR}")

    has_errors = any(s["errors"] > 0 for s in all_stats.values())
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
