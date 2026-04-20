"""Helpers for locating downloaded Salesforce binary files per record.

The downloader (``scripts/download_files.py``) writes files to
``data/sf_files/`` and maintains ``manifest.json`` mapping every
``Attachment`` / ``ContentVersion`` / ``Document`` Id to its on-disk path.

This module reads that manifest plus the SQLite DB to enumerate files
attached to a given record, so ``record_view`` can display a "Files"
panel with download links.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

# ``sf_browser/`` is a sibling of ``data/`` — walk up one level to the repo root.
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

FILES_DIR = DATA_DIR / "sf_files"
MANIFEST_PATH = FILES_DIR / "manifest.json"


@lru_cache(maxsize=1)
def _load_manifest() -> dict[str, dict[str, Any]]:
    """Load the sf_files manifest once per process."""
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def manifest_is_available() -> bool:
    """True if ``data/sf_files/manifest.json`` exists and parses."""
    return bool(_load_manifest())


def files_for_record(
    conn: sqlite3.Connection, object_name: str, record_id: str
) -> list[dict[str, Any]]:
    """Return all downloaded files attached or directly representing a record.

    Sources covered:
      * Record itself IS a file: ``Attachment``, ``ContentVersion``,
        ``Document``, ``ContentDocument``.
      * ``Attachment`` where ``ParentId = record_id``.
      * ``ContentDocumentLink`` where ``LinkedEntityId = record_id`` →
        latest ``ContentVersion`` per linked ``ContentDocument``.
      * ``SDOC__SDoc__c`` where ``SDOC__ObjectID__c = record_id`` →
        SDocs-generated PDFs (invoices, quotes, contracts).

    Each returned item:
        {id, source_object, type, name, size, content_type, created_date,
         path, missing, note}
    """
    manifest = _load_manifest()
    results: list[dict[str, Any]] = []

    # 1. Record itself represents a file.
    if object_name == "Attachment":
        try:
            r = conn.execute(
                'SELECT Id, ParentId, Name, ContentType, BodyLength, CreatedDate '
                'FROM "Attachment" WHERE Id = ?',
                (record_id,),
            ).fetchone()
        except sqlite3.DatabaseError:
            r = None
        if r:
            entry = manifest.get(r["Id"]) or {}
            results.append(
                {
                    "id": r["Id"],
                    "source_object": "Attachment (self)",
                    "type": "attachment",
                    "name": r["Name"],
                    "size": r["BodyLength"] or 0,
                    "content_type": r["ContentType"],
                    "created_date": r["CreatedDate"],
                    "path": entry.get("path"),
                    "missing": not entry.get("path"),
                    "parent_id": r["ParentId"],
                }
            )
    elif object_name == "ContentVersion":
        try:
            r = conn.execute(
                'SELECT Id, ContentDocumentId, Title, FileExtension, ContentSize, CreatedDate '
                'FROM "ContentVersion" WHERE Id = ?',
                (record_id,),
            ).fetchone()
        except sqlite3.DatabaseError:
            r = None
        if r:
            entry = manifest.get(r["Id"]) or {}
            ext = r["FileExtension"] or ""
            name = r["Title"] + (f".{ext}" if ext and not r["Title"].lower().endswith("." + ext.lower()) else "")
            results.append(
                {
                    "id": r["Id"],
                    "source_object": "ContentVersion (self)",
                    "type": "content",
                    "name": name,
                    "size": r["ContentSize"] or 0,
                    "content_type": None,
                    "created_date": r["CreatedDate"],
                    "path": entry.get("path"),
                    "missing": not entry.get("path"),
                }
            )
    elif object_name == "Document":
        try:
            r = conn.execute(
                'SELECT Id, Name, ContentType, BodyLength, CreatedDate '
                'FROM "Document" WHERE Id = ?',
                (record_id,),
            ).fetchone()
        except sqlite3.DatabaseError:
            r = None
        if r:
            entry = manifest.get(r["Id"]) or {}
            results.append(
                {
                    "id": r["Id"],
                    "source_object": "Document (self)",
                    "type": "document",
                    "name": r["Name"],
                    "size": r["BodyLength"] or 0,
                    "content_type": r["ContentType"],
                    "created_date": r["CreatedDate"],
                    "path": entry.get("path"),
                    "missing": not entry.get("path"),
                }
            )

    # 2. Classic Attachments where this record is the parent.
    if object_name != "Attachment":
        try:
            rows = conn.execute(
                'SELECT Id, Name, ContentType, BodyLength, CreatedDate '
                'FROM "Attachment" WHERE ParentId = ? ORDER BY CreatedDate DESC',
                (record_id,),
            ).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        for r in rows:
            entry = manifest.get(r["Id"]) or {}
            results.append(
                {
                    "id": r["Id"],
                    "source_object": "Attachment",
                    "type": "attachment",
                    "name": r["Name"],
                    "size": r["BodyLength"] or 0,
                    "content_type": r["ContentType"],
                    "created_date": r["CreatedDate"],
                    "path": entry.get("path"),
                    "missing": not entry.get("path"),
                }
            )

    # 3. Lightning ContentVersions via ContentDocumentLink.
    try:
        links = conn.execute(
            'SELECT ContentDocumentId FROM "ContentDocumentLink" '
            'WHERE LinkedEntityId = ?',
            (record_id,),
        ).fetchall()
    except sqlite3.DatabaseError:
        links = []
    if links:
        doc_ids = [r["ContentDocumentId"] for r in links if r["ContentDocumentId"]]
        if doc_ids:
            placeholders = ",".join("?" for _ in doc_ids)
            try:
                cvs = conn.execute(
                    'SELECT Id, ContentDocumentId, Title, FileExtension, '
                    'ContentSize, CreatedDate '
                    'FROM "ContentVersion" '
                    f'WHERE ContentDocumentId IN ({placeholders}) '
                    'AND IsLatest = 1 '
                    'ORDER BY CreatedDate DESC',
                    doc_ids,
                ).fetchall()
            except sqlite3.DatabaseError:
                cvs = []
            for r in cvs:
                entry = manifest.get(r["Id"]) or {}
                ext = r["FileExtension"] or ""
                display_name = r["Title"]
                if ext and not display_name.lower().endswith("." + ext.lower()):
                    display_name = f"{display_name}.{ext}"
                results.append(
                    {
                        "id": r["Id"],
                        "source_object": "ContentVersion",
                        "type": "content",
                        "name": display_name,
                        "size": r["ContentSize"] or 0,
                        "content_type": None,
                        "created_date": r["CreatedDate"],
                        "path": entry.get("path"),
                        "missing": not entry.get("path"),
                    }
                )

    # 4a. SDJob → related SDocs: an SDJob runs the SDocs generator, and the
    # resulting PDF lives on the associated SDOC__SDoc__c row via SDoc1/SDoc2.
    if object_name == "SDOC__SDJob__c":
        try:
            job = conn.execute(
                'SELECT SDOC__SDoc1__c AS sdoc1, SDOC__SDoc2__c AS sdoc2 '
                'FROM "SDOC__SDJob__c" WHERE Id = ?',
                (record_id,),
            ).fetchone()
        except sqlite3.DatabaseError:
            job = None
        sdoc_ids = [job[k] for k in ("sdoc1", "sdoc2") if job and job[k]] if job else []
        if sdoc_ids:
            placeholders = ",".join("?" for _ in sdoc_ids)
            try:
                sd_rows = conn.execute(
                    'SELECT Id, SDOC__File_ID__c AS cv_id, '
                    'SDOC__Attachment_Name__c AS filename, '
                    'SDOC__Document_Number__c AS doc_num, '
                    'SDOC__Document_Name__c AS doc_name, '
                    'CreatedDate, SDOC__Status__c AS status '
                    'FROM "SDOC__SDoc__c" '
                    f'WHERE Id IN ({placeholders}) AND SDOC__File_ID__c IS NOT NULL '
                    "AND SDOC__File_ID__c != ''",
                    sdoc_ids,
                ).fetchall()
            except sqlite3.DatabaseError:
                sd_rows = []
            for s in sd_rows:
                cv = s["cv_id"]
                entry = manifest.get(cv) or {}
                name = s["filename"] or f"{s['doc_num'] or cv}.pdf"
                results.append(
                    {
                        "id": cv,
                        "source_object": f"SDocs ({s['doc_name'] or 'PDF'})",
                        "type": "content",
                        "name": name,
                        "size": entry.get("size", 0),
                        "content_type": "application/pdf",
                        "created_date": s["CreatedDate"],
                        "path": entry.get("path"),
                        "missing": not entry.get("path"),
                        "sdoc_id": s["Id"],
                        "status": s["status"],
                    }
                )

    # 4b. SDocs-generated PDFs (invoices, quotes, contracts) attached via
    # the SDOC package. SDOC__ObjectID__c points to the parent record.
    try:
        sdocs = conn.execute(
            'SELECT Id, SDOC__File_ID__c AS cv_id, '
            'SDOC__Attachment_Name__c AS filename, '
            'SDOC__Document_Number__c AS doc_num, '
            'SDOC__Document_Name__c AS doc_name, '
            'CreatedDate, SDOC__Status__c AS status '
            'FROM "SDOC__SDoc__c" '
            'WHERE SDOC__ObjectID__c = ? AND SDOC__File_ID__c IS NOT NULL '
            "AND SDOC__File_ID__c != '' "
            'ORDER BY CreatedDate DESC',
            (record_id,),
        ).fetchall()
    except sqlite3.DatabaseError:
        sdocs = []
    for s in sdocs:
        cv = s["cv_id"]
        entry = manifest.get(cv) or {}
        name = s["filename"] or f"{s['doc_num'] or cv}.pdf"
        results.append(
            {
                "id": cv,
                "source_object": f"SDocs ({s['doc_name'] or 'PDF'})",
                "type": "content",
                "name": name,
                "size": entry.get("size", 0),
                "content_type": "application/pdf",
                "created_date": s["CreatedDate"],
                "path": entry.get("path"),
                "missing": not entry.get("path"),
                "sdoc_id": s["Id"],
                "status": s["status"],
            }
        )

    return results


def resolve_file_path(file_id: str) -> Path | None:
    """Return the on-disk path for a manifest entry Id, if downloaded."""
    entry = _load_manifest().get(file_id)
    if not entry:
        return None
    rel = entry.get("path")
    if not rel:
        return None
    p = FILES_DIR / rel
    if not p.exists() or p.stat().st_size == 0:
        return None
    # Safety: ensure resolved path stays under FILES_DIR (no ../ traversal).
    try:
        p.resolve().relative_to(FILES_DIR.resolve())
    except ValueError:
        return None
    return p


def lookup_manifest_entry(file_id: str) -> dict[str, Any] | None:
    """Return the raw manifest entry for a file Id (for display filename)."""
    return _load_manifest().get(file_id)
