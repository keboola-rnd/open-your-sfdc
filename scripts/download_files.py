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

    # Query all latest versions. FirstPublishLocationId is the record a file was
    # first published to (Order/Opportunity/Account/…) — a free partial
    # file->record link that does not depend on ContentDocumentLink visibility.
    # See docs/enhancement-file-to-record-links.md §R3.
    soql = (
        "SELECT Id, ContentDocumentId, Title, FileExtension, ContentSize, "
        "PathOnClient, VersionNumber, CreatedDate, CreatedById, "
        "Description, IsLatest, FirstPublishLocationId "
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
        # Collision guard: multiple ContentVersions can share a filename within
        # the same ContentDocument (e.g. multiple revisions or SDocs PDFs with
        # the same Title). Prefix with the CV Id short form to disambiguate.
        filepath = subdir / f"{cv_id[:15]}__{filename}"

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
                "first_publish_location_id": rec.get("FirstPublishLocationId"),
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

    # DocuSign for Salesforce (dfsle) — each dfsle__Document__c row of
    # type 'ContentVersion' references an Order Form / signed PDF / template
    # via dfsle__SourceId__c. Most rows store a *ContentDocument* Id (069...
    # prefix) which has to be resolved to the latest ContentVersion via SOQL,
    # the same dance ContentAsset uses. A handful store a ContentVersion Id
    # directly (068... prefix) and can be downloaded as-is.
    docusign_cv_targets: set[str] = set()      # 068... — direct CV
    docusign_cd_targets: set[str] = set()      # 069... — ContentDocument, resolve later
    docusign_metadata: dict[str, dict] = {}    # keyed by source_id (068 or 069)
    try:
        rows = conn.execute(
            'SELECT d.Id AS doc_id, '
            'd.dfsle__SourceId__c AS source_id, '
            'd.Name AS filename, '
            'd.dfsle__Extension__c AS ext, '
            'd.dfsle__Sequence__c AS seq, '
            'd.dfsle__Envelope__c AS envelope_id, '
            'e.dfsle__SourceId__c AS parent_id '
            'FROM "dfsle__Document__c" d '
            'LEFT JOIN "dfsle__Envelope__c" e ON e.Id = d.dfsle__Envelope__c '
            "WHERE d.dfsle__Type__c = 'ContentVersion' "
            "AND d.dfsle__SourceId__c IS NOT NULL "
            "AND d.dfsle__SourceId__c != ''"
        ).fetchall()
        for r in rows:
            sid = r["source_id"]
            if not sid:
                continue
            md = {
                "doc_id": r["doc_id"],
                "filename": r["filename"] or sid,
                "extension": r["ext"] or "",
                "envelope_id": r["envelope_id"],
                "parent_id": r["parent_id"],
                "sequence": r["seq"],
            }
            # Keep first-seen metadata per source id (envelopes may share refs).
            docusign_metadata.setdefault(sid, md)
            if sid.startswith("069"):
                docusign_cd_targets.add(sid)
            elif sid.startswith("068") and sid not in cv_ids:
                docusign_cv_targets.add(sid)
    except sqlite3.DatabaseError:
        pass

    conn.close()

    docusign_total = len(docusign_cv_targets) + len(docusign_cd_targets)
    total = (len(fa_targets) + len(ca_doc_targets)
             + len(sdoc_targets) + docusign_total)
    print(f"Found {len(fa_targets)} chatter ContentVersion refs, "
          f"{len(ca_doc_targets)} ContentAsset ContentDocument refs, "
          f"{len(sdoc_targets)} SDocs PDFs (invoices/quotes), "
          f"{docusign_total} DocuSign documents (Order Forms / contracts; "
          f"{len(docusign_cd_targets)} CD + {len(docusign_cv_targets)} CV)")
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

    # Resolve DocuSign ContentDocument refs → latest CV ids via SOQL.
    # docusign_metadata is keyed by source_id (069...); after resolution we
    # rekey it to the resolved cv_id so the download loop can find it.
    docusign_targets: set[str] = set(docusign_cv_targets)
    if docusign_cd_targets:
        print(f"  Resolving {len(docusign_cd_targets)} DocuSign ContentDocuments to latest ContentVersions...")
        cd_ids_list = list(docusign_cd_targets)
        for chunk_start in range(0, len(cd_ids_list), 200):
            chunk = cd_ids_list[chunk_start:chunk_start + 200]
            placeholders = ",".join(f"'{d}'" for d in chunk)
            soql = (
                f"SELECT Id, ContentDocumentId, Title, FileExtension, "
                f"ContentSize, CreatedDate FROM ContentVersion "
                f"WHERE ContentDocumentId IN ({placeholders}) AND IsLatest = true"
            )
            try:
                res = sf.query_all(soql)
                for rec in res.get("records", []):
                    cv_id = rec["Id"]
                    cd_id = rec["ContentDocumentId"]
                    docusign_targets.add(cv_id)
                    # Move metadata from CD-key to CV-key so the loop finds it.
                    if cd_id in docusign_metadata:
                        docusign_metadata[cv_id] = docusign_metadata.pop(cd_id)
                    # Also keep the SF-side meta for size/createdDate fallback.
                    manifest.setdefault(f"__ca_meta_{cv_id}", rec)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! DocuSign lookup chunk failed: {exc}")
        resolved_cd_count = sum(1 for k in docusign_metadata if k.startswith("068"))
        print(f"    resolved: {resolved_cd_count}, "
              f"unresolved: {len(docusign_cd_targets) - resolved_cd_count} "
              f"(CV deleted or no access)")

    # Merge all CV targets into a single download queue, carrying known metadata
    # so we can name SDocs PDFs properly (301000587.pdf instead of Id-only).
    all_targets = fa_targets | sdoc_targets | docusign_targets
    for cv_id, md in sdoc_metadata.items():
        manifest.setdefault(f"__sdoc_meta_{cv_id}", md)
    for cv_id, md in docusign_metadata.items():
        manifest.setdefault(f"__docusign_meta_{cv_id}", md)

    target_list = sorted(all_targets)
    print(f"  Downloading {len(target_list)} external ContentVersions...")

    for i, cv_id in enumerate(target_list):
        meta = manifest.get(f"__ca_meta_{cv_id}") or {}
        sdoc_md = manifest.get(f"__sdoc_meta_{cv_id}") or {}
        docusign_md = manifest.get(f"__docusign_meta_{cv_id}") or {}
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

        # DocuSign: filename + extension come from dfsle__Document__c.
        if docusign_md.get("filename"):
            title = docusign_md["filename"]
            ext = docusign_md.get("extension") or ext
            # Strip extension from name if it was duplicated (e.g. "Foo.docx").
            if ext and title.lower().endswith(f".{ext.lower()}"):
                title = title[: -(len(ext) + 1)]
            # Group signed envelope artifacts under the parent record (Order/Opp).
            doc_id = docusign_md.get("parent_id") or docusign_md.get("envelope_id") or doc_id

        # Look up metadata if we don't have it (FeedAttachment path).
        if not ext and not sdoc_md and not docusign_md:
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
        # Collision guard: SDocs PDFs and other external refs frequently share
        # filenames (Invoice-0000024.pdf etc.). Prefix with CV Id to keep them
        # distinct on disk.
        filepath = subdir / f"{cv_id[:15]}__{filename}"

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
                "source": ("sdoc" if sdoc_md
                           else "docusign" if docusign_md
                           else "external_ref"),
                **({"sdoc_id": sdoc_md["sdoc_id"],
                    "parent_id": sdoc_md.get("parent_id"),
                    "parent_type": sdoc_md.get("parent_type")} if sdoc_md else {}),
                **({"docusign_doc_id": docusign_md["doc_id"],
                    "envelope_id": docusign_md.get("envelope_id"),
                    "parent_id": docusign_md.get("parent_id"),
                    "sequence": docusign_md.get("sequence")} if docusign_md else {}),
            }
            if (i + 1) % 10 == 0:
                print(f"  [{i + 1}/{total}] Downloaded {title}.{ext}")
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"  ! [{i + 1}] {cv_id}: {exc}")

    # Strip cache entries.
    for key in list(manifest.keys()):
        if (key.startswith("__ca_meta_")
                or key.startswith("__sdoc_meta_")
                or key.startswith("__docusign_meta_")):
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
        # Collision guard: classic Documents in the same folder can share names.
        # Prefix with Document Id short form to disambiguate.
        filepath = subdir / f"{doc_id[:15]}__{filename}"

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


# Salesforce key-prefix -> object type, used to type a linked record id. Only
# the prefixes relevant to file->record links are mapped; anything else reports
# as "Unknown" so a consumer can extend this table. Custom objects share the
# dynamic prefix space and can't be named from the prefix alone.
SF_PREFIX_TO_TYPE: dict[str, str] = {
    "001": "Account",
    "003": "Contact",
    "005": "User",
    "006": "Opportunity",
    "00D": "Organization",
    "500": "Case",
    "701": "Campaign",
    "800": "Contract",
    "801": "Order",
}

# When a ContentDocument is shared with several records, prefer the most
# business-meaningful link. Types not listed (custom objects, Unknown) rank
# between these and the de-prioritised User/Organization shares.
_LINK_TYPE_PRIORITY = ["Order", "Contract", "Opportunity", "Account", "Case", "Campaign", "Contact"]


def _entity_type(entity_id: str | None) -> str | None:
    """Map a Salesforce id to its object type via key prefix (best effort)."""
    if not entity_id or len(entity_id) < 3:
        return None
    return SF_PREFIX_TO_TYPE.get(entity_id[:3], "Unknown")


def _pick_primary_link(entity_ids: list[str]) -> str | None:
    """Choose the most business-meaningful link from a file's CDL entries.

    Business records (Order, Contract, Opportunity, …) win over custom/unknown
    objects, which in turn win over the User/Organization shares that every
    file carries. Ties break on the id itself for determinism.
    """
    if not entity_ids:
        return None

    def rank(eid: str) -> tuple[int, int, str]:
        etype = _entity_type(eid)
        if etype in _LINK_TYPE_PRIORITY:
            return (0, _LINK_TYPE_PRIORITY.index(etype), eid)
        if etype in ("User", "Organization"):
            return (2, 0, eid)
        return (1, 0, eid)  # custom / unknown business object

    return sorted(entity_ids, key=rank)[0]


def enrich_manifest_with_record_links(
    manifest: dict, *, db_path: Path | None = None
) -> dict[str, Any]:
    """Populate a typed linked_entity_id/type on every file entry and
    de-overload content_document_id.

    Precedence per ContentVersion file (manifest key = ContentVersion Id):
      1. ContentDocumentLink (full after the export-side R1/R2 fix) — the only
         source that recovers the signed-PDF -> Order link DocuSign writes back
         as a normal ContentVersion.
      2. FirstPublishLocationId (R3) — partial, visibility-independent fallback.
      3. DocuSign / SDoc parent_id already stashed on the entry — source
         documents that have no ContentDocumentLink row.

    content_document_id is rewritten to the real 069 id (from the DB, or an
    existing 069-looking value) or null — it no longer carries an overloaded
    parent/envelope id. Classic Attachments are linked via their direct ParentId.

    Reads the local SQLite DB read-only; if it (or a needed table) is absent the
    function logs and returns without touching link fields, so a standalone
    ``download_files.py`` run that skipped the import step degrades gracefully.
    See docs/enhancement-file-to-record-links.md §R4.
    """
    print("\n=== Enrich manifest with record links ===")
    import sqlite3

    stats = {
        "content_document_link": 0,
        "first_publish_location": 0,
        "parent": 0,
        "attachment_parent": 0,
        "unlinked": 0,
    }

    if db_path is None:
        db_path = DATA_DIR / "salesforce_full.db"
    if not db_path.exists():
        print(f"  (DB not found at {db_path} — skipping link enrichment)")
        return stats

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    cv_to_cd: dict[str, str] = {}
    cv_to_fpl: dict[str, str] = {}
    cd_to_links: dict[str, list[str]] = {}
    try:
        # Probe the schema first. A DB imported before R3 added
        # FirstPublishLocationId to the CV export won't have that column — and
        # SQLite would silently read a double-quoted missing column as a string
        # literal, then raise IndexError on row access. So we select only the
        # columns that actually exist. A missing ContentVersion table yields an
        # empty PRAGMA, leaving the maps empty (graceful no-op). Selecting only
        # present columns also keeps cv_to_cd populated when FPL is absent, so
        # the content_document_id de-overload still resolves real 069 ids.
        cv_cols = {row[1] for row in conn.execute('PRAGMA table_info("ContentVersion")')}
        if cv_cols:
            has_fpl = "FirstPublishLocationId" in cv_cols
            select_cols = '"Id", "ContentDocumentId"' + (
                ', "FirstPublishLocationId"' if has_fpl else ""
            )
            for r in conn.execute(f"SELECT {select_cols} FROM ContentVersion"):
                if r["ContentDocumentId"]:
                    cv_to_cd[r["Id"]] = r["ContentDocumentId"]
                if has_fpl and r["FirstPublishLocationId"]:
                    cv_to_fpl[r["Id"]] = r["FirstPublishLocationId"]
    except sqlite3.OperationalError as exc:
        print(f"  (ContentVersion not queryable: {exc})")
    try:
        for r in conn.execute(
            'SELECT "ContentDocumentId", "LinkedEntityId" FROM ContentDocumentLink '
            "WHERE \"LinkedEntityId\" IS NOT NULL AND \"LinkedEntityId\" != ''"
        ):
            cd_to_links.setdefault(r["ContentDocumentId"], []).append(r["LinkedEntityId"])
    except sqlite3.OperationalError as exc:
        print(f"  (ContentDocumentLink not queryable: {exc})")
    conn.close()

    print(
        f"  loaded {len(cv_to_cd):,} CV->CD, {len(cv_to_fpl):,} CV->FPL, "
        f"{sum(len(v) for v in cd_to_links.values()):,} links over {len(cd_to_links):,} docs"
    )

    for key, entry in manifest.items():
        if key.startswith("__") or not isinstance(entry, dict):
            continue
        etype = entry.get("type")

        if etype == "Attachment":
            # Classic attachments carry a direct ParentId — already a record link.
            pid = entry.get("parent_id")
            if pid:
                entry["linked_entity_id"] = pid
                entry["linked_entity_type"] = _entity_type(pid)
                entry["linked_entity_source"] = "attachment_parent"
                stats["attachment_parent"] += 1
            else:
                stats["unlinked"] += 1
            continue

        if etype != "ContentVersion":
            continue

        cv_id = key
        # Real ContentDocument id: DB first, then an existing 069-looking value.
        real_cd = cv_to_cd.get(cv_id)
        if not real_cd:
            existing = entry.get("content_document_id")
            if isinstance(existing, str) and existing.startswith("069"):
                real_cd = existing

        cdl_links = cd_to_links.get(real_cd, []) if real_cd else []
        primary = _pick_primary_link(cdl_links)
        fpl = entry.get("first_publish_location_id") or cv_to_fpl.get(cv_id)
        parent = entry.get("parent_id")

        # Precedence is literal per spec §R4: ContentDocumentLink, then
        # FirstPublishLocationId, then DocuSign/SDoc parent. A file whose only
        # CDL link is a User/Organization share therefore keeps that share
        # rather than falling through to FPL — rare, and the signed-PDF -> Order
        # goal is unaffected (those carry the Order link in the CDL).
        if primary:
            entry["linked_entity_id"] = primary
            entry["linked_entity_type"] = _entity_type(primary)
            entry["linked_entity_source"] = "content_document_link"
            if len(set(cdl_links)) > 1:
                entry["linked_entities"] = sorted(set(cdl_links))
            stats["content_document_link"] += 1
        elif fpl:
            entry["linked_entity_id"] = fpl
            entry["linked_entity_type"] = _entity_type(fpl)
            entry["linked_entity_source"] = "first_publish_location"
            stats["first_publish_location"] += 1
        elif parent:
            entry["linked_entity_id"] = parent
            entry["linked_entity_type"] = entry.get("parent_type") or _entity_type(parent)
            entry["linked_entity_source"] = (
                "sdoc_parent" if entry.get("source") == "sdoc" else "docusign_parent"
            )
            stats["parent"] += 1
        else:
            entry["linked_entity_id"] = None
            entry["linked_entity_type"] = None
            entry["linked_entity_source"] = None
            stats["unlinked"] += 1

        # De-overload: content_document_id is now the real 069 id (or null).
        entry["content_document_id"] = real_cd

    print(
        f"  linked: {stats['content_document_link']:,} via ContentDocumentLink, "
        f"{stats['first_publish_location']:,} via FirstPublishLocation, "
        f"{stats['parent']:,} via parent, "
        f"{stats['attachment_parent']:,} attachments; "
        f"{stats['unlinked']:,} still unlinked"
    )
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

    # Save manifest. Enrich first: join the downloaded binaries to their records
    # via the (now complete) ContentDocumentLink in the DB, falling back to
    # FirstPublishLocationId / parent ids. Requires the import step to have run.
    # (Not folded into all_stats — the summary loop below assumes download-shaped
    # stats; enrich logs its own counts.)
    if not args.dry_run:
        enrich_manifest_with_record_links(manifest)
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
