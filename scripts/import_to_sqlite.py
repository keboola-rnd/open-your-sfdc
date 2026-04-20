#!/usr/bin/env python3
"""
Universal SQLite import for the full Salesforce export.

Reads ``data/sf_full_export/metadata.json`` and the matching per-object CSV
files, auto-generates SQLite tables from the SF field metadata, imports all
records, and creates helpful indexes (PK on Id, indexes on reference fields,
optional FTS indexes on text columns).

Usage:
    python scripts/import_to_sqlite.py
    python scripts/import_to_sqlite.py --db data/my_export.db
    python scripts/import_to_sqlite.py --no-fts
    python scripts/import_to_sqlite.py --export-dir data/sf_full_export
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"

DEFAULT_EXPORT_DIR = DATA_DIR / "sf_full_export"
DEFAULT_DB_PATH = DATA_DIR / "salesforce_full.db"

# CSV field size can exceed the default csv limit for huge text fields
# (e.g. long HTML descriptions). Bump it sky-high.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

SF_TO_SQLITE: dict[str, str] = {
    "id": "TEXT",
    "reference": "TEXT",
    "string": "TEXT",
    "textarea": "TEXT",
    "picklist": "TEXT",
    "multipicklist": "TEXT",
    "boolean": "INTEGER",
    "int": "INTEGER",
    "long": "INTEGER",
    "double": "REAL",
    "currency": "REAL",
    "percent": "REAL",
    "date": "TEXT",
    "datetime": "TEXT",
    "time": "TEXT",
    "email": "TEXT",
    "phone": "TEXT",
    "url": "TEXT",
    "base64": "BLOB",
    "anyType": "TEXT",
    "combobox": "TEXT",
    "encryptedstring": "TEXT",
    "address": "TEXT",
    "location": "TEXT",
}

# Reserved-ish SQLite words commonly seen as SF field names. We always quote
# every identifier anyway, but a small set to be paranoid about.
SQLITE_RESERVED = {
    "order",
    "group",
    "from",
    "to",
    "select",
    "where",
    "when",
    "table",
}


def quote_ident(name: str) -> str:
    """Quote a SQLite identifier with double quotes, escaping embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def sqlite_type_for(sf_type: str) -> str:
    return SF_TO_SQLITE.get(sf_type, "TEXT")


def convert_value(value: str, sf_type: str) -> Any:
    """Convert a CSV string value to a SQLite-friendly Python value."""
    if value is None or value == "":
        return None

    if sf_type == "boolean":
        return 1 if value.lower() in ("true", "1", "yes") else 0

    if sf_type in ("int", "long"):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    if sf_type in ("double", "currency", "percent"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return value


def build_create_table(
    object_name: str,
    fields: list[dict],
    extra_csv_columns: list[str],
) -> str:
    """Return a CREATE TABLE statement for a Salesforce object."""
    cols: list[str] = []
    seen: set[str] = set()
    for field in fields:
        name = field["name"]
        if name in seen:
            continue
        seen.add(name)
        sqlite_type = sqlite_type_for(field.get("type", "string") or "string")
        pk = " PRIMARY KEY" if name == "Id" else ""
        cols.append(f"{quote_ident(name)} {sqlite_type}{pk}")

    # Extra CSV columns are nested relationship lookups like "Account.Name" that
    # the exporter flattened but that aren't in the object's own describe fields.
    for extra in extra_csv_columns:
        if extra in seen:
            continue
        seen.add(extra)
        cols.append(f"{quote_ident(extra)} TEXT")

    body = ",\n  ".join(cols)
    return f"CREATE TABLE IF NOT EXISTS {quote_ident(object_name)} (\n  {body}\n);"


def create_metadata_tables(conn: sqlite3.Connection) -> None:
    """Create the meta tables that describe the export."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS _sf_objects (
            name TEXT PRIMARY KEY,
            label TEXT,
            custom INTEGER,
            key_prefix TEXT,
            record_count INTEGER,
            exported_at TEXT
        );

        CREATE TABLE IF NOT EXISTS _sf_fields (
            object_name TEXT,
            field_name TEXT,
            label TEXT,
            sf_type TEXT,
            sqlite_type TEXT,
            length INTEGER,
            reference_to TEXT,
            relationship_name TEXT,
            custom INTEGER,
            nillable INTEGER,
            PRIMARY KEY (object_name, field_name)
        );

        CREATE TABLE IF NOT EXISTS _sf_relationships (
            from_object TEXT,
            from_field TEXT,
            to_object TEXT,
            relationship_name TEXT,
            PRIMARY KEY (from_object, from_field, to_object)
        );
        """
    )


def populate_metadata_tables(
    conn: sqlite3.Connection,
    metadata: dict,
) -> None:
    export_date = metadata.get("export_date")
    objects = metadata.get("objects", {})

    conn.execute("DELETE FROM _sf_objects")
    conn.execute("DELETE FROM _sf_fields")
    conn.execute("DELETE FROM _sf_relationships")

    for name, obj in objects.items():
        conn.execute(
            "INSERT INTO _sf_objects (name, label, custom, key_prefix, record_count, exported_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                obj.get("label"),
                1 if obj.get("custom") else 0,
                obj.get("key_prefix"),
                obj.get("record_count", 0),
                export_date,
            ),
        )
        for field in obj.get("fields", []):
            refs = field.get("referenceTo") or []
            conn.execute(
                "INSERT OR REPLACE INTO _sf_fields "
                "(object_name, field_name, label, sf_type, sqlite_type, length, "
                " reference_to, relationship_name, custom, nillable) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    field.get("name"),
                    field.get("label"),
                    field.get("type"),
                    sqlite_type_for(field.get("type", "string") or "string"),
                    field.get("length"),
                    ",".join(refs) if refs else None,
                    field.get("relationshipName"),
                    1 if field.get("custom") else 0,
                    1 if field.get("nillable") else 0,
                ),
            )
            for ref in refs:
                if not ref:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO _sf_relationships "
                    "(from_object, from_field, to_object, relationship_name) "
                    "VALUES (?, ?, ?, ?)",
                    (name, field.get("name"), ref, field.get("relationshipName")),
                )

    conn.commit()


def read_csv_header(csv_path: Path) -> list[str]:
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration:
            return []


def import_object(
    conn: sqlite3.Connection,
    object_name: str,
    fields: list[dict],
    csv_path: Path,
    batch_size: int = 1000,
) -> int:
    """Create the table and load rows from ``csv_path``. Returns rows inserted."""
    header = read_csv_header(csv_path)
    if not header:
        print(f"  {object_name}: CSV missing header, skipping")
        return 0

    field_types = {f["name"]: (f.get("type") or "string") for f in fields}
    # Columns that appeared in the CSV but aren't in the object's own fields
    # (e.g. flattened parent lookups like "Account.Name").
    extra_columns = [h for h in header if h not in field_types]

    ddl = build_create_table(object_name, fields, extra_columns)
    conn.execute(f"DROP TABLE IF EXISTS {quote_ident(object_name)}")
    conn.execute(ddl)

    table_columns = [f["name"] for f in fields] + [c for c in extra_columns if c not in field_types]
    # Drop columns in CSV header that we didn't declare on the table (shouldn't
    # happen because extras are included, but guard defensively).
    insert_cols = [c for c in header if c in table_columns]

    col_list = ", ".join(quote_ident(c) for c in insert_cols)
    placeholders = ", ".join("?" for _ in insert_cols)
    insert_sql = (
        f"INSERT OR REPLACE INTO {quote_ident(object_name)} ({col_list}) VALUES ({placeholders})"
    )

    count = 0
    batch: list[tuple] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            values = []
            for col in insert_cols:
                raw = row.get(col, "")
                ftype = field_types.get(col, "string")
                values.append(convert_value(raw, ftype))
            batch.append(tuple(values))
            if len(batch) >= batch_size:
                conn.executemany(insert_sql, batch)
                count += len(batch)
                batch = []
        if batch:
            conn.executemany(insert_sql, batch)
            count += len(batch)
    conn.commit()
    return count


def create_indexes(
    conn: sqlite3.Connection,
    object_name: str,
    fields: list[dict],
) -> None:
    """Create indexes on reference-type fields to speed up joins/lookups."""
    for field in fields:
        if field.get("type") != "reference":
            continue
        fname = field["name"]
        index_name = f"idx_{object_name}_{fname}".replace(".", "_")
        sql = (
            f"CREATE INDEX IF NOT EXISTS {quote_ident(index_name)} "
            f"ON {quote_ident(object_name)} ({quote_ident(fname)})"
        )
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as exc:
            print(f"  ! index {index_name} failed: {exc}")
    conn.commit()


def create_fts_index(
    conn: sqlite3.Connection,
    object_name: str,
    fields: list[dict],
    max_columns: int = 10,
) -> None:
    """Create a FTS5 virtual table indexing the key text columns for search."""
    text_types = {"string", "textarea", "email", "phone", "url", "picklist"}
    text_fields = [f["name"] for f in fields if f.get("type") in text_types]
    # Always include Name if present so record lookups match by human labels.
    if "Name" in [f["name"] for f in fields] and "Name" not in text_fields:
        text_fields.insert(0, "Name")
    if not text_fields:
        return
    selected = text_fields[:max_columns]
    fts_table = f"_fts_{object_name}"

    conn.execute(f"DROP TABLE IF EXISTS {quote_ident(fts_table)}")
    try:
        cols_sql = ", ".join(quote_ident(c) for c in selected)
        conn.execute(
            f"CREATE VIRTUAL TABLE {quote_ident(fts_table)} USING fts5("
            f"sf_id UNINDEXED, {cols_sql}, tokenize='unicode61 remove_diacritics 2')"
        )
        # Populate the FTS index.
        src_cols = ", ".join(quote_ident(c) for c in selected)
        conn.execute(
            f"INSERT INTO {quote_ident(fts_table)} (sf_id, {cols_sql}) "
            f"SELECT {quote_ident('Id')}, {src_cols} FROM {quote_ident(object_name)}"
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        # FTS5 may be unavailable; fail soft.
        print(f"  ! FTS index for {object_name} not created: {exc}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Target SQLite database (default: {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=DEFAULT_EXPORT_DIR,
        help=f"Directory with export CSV + metadata.json (default: {DEFAULT_EXPORT_DIR}).",
    )
    parser.add_argument(
        "--no-fts",
        action="store_true",
        help="Skip creating FTS5 full-text-search indexes (faster import).",
    )
    parser.add_argument(
        "--objects",
        type=str,
        default=None,
        help="Comma-separated list of object API names to import (default: all).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    metadata_path = args.export_dir / "metadata.json"
    if not metadata_path.exists():
        print(f"Error: {metadata_path} not found. Run export_all.py first.")
        return 1

    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    objects = metadata.get("objects", {})
    if not objects:
        print("No objects in metadata.json -- nothing to import.")
        return 0

    if args.objects:
        wanted = {n.strip() for n in args.objects.split(",") if n.strip()}
        objects = {k: v for k, v in objects.items() if k in wanted}

    print("=" * 60)
    print("Universal SQLite Import")
    print("=" * 60)
    print(f"Source: {args.export_dir}")
    print(f"Target DB: {args.db}")
    print(f"Objects to import: {len(objects)}")

    # Fresh DB every time -- schema is derived from metadata, no migration needed.
    if args.db.exists():
        args.db.unlink()
    args.db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    try:
        conn.execute("PRAGMA journal_mode = MEMORY")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")

        create_metadata_tables(conn)
        populate_metadata_tables(conn, metadata)

        total_rows = 0
        total_ok = 0
        total_err = 0

        for name, obj in sorted(objects.items()):
            csv_path = args.export_dir / f"{name}.csv"
            if not csv_path.exists():
                print(f"  {name}: CSV missing, skipping")
                continue
            fields = obj.get("fields", [])
            try:
                rows = import_object(conn, name, fields, csv_path)
                create_indexes(conn, name, fields)
                if not args.no_fts:
                    create_fts_index(conn, name, fields)
                total_rows += rows
                total_ok += 1
                print(f"  {name}: imported {rows:,} rows")
            except Exception as exc:  # noqa: BLE001
                total_err += 1
                print(f"  ! {name} failed: {type(exc).__name__}: {exc}")

        conn.execute("PRAGMA journal_mode = DELETE")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    print("\n" + "=" * 60)
    print(f"Done. ok={total_ok} errors={total_err} total_rows={total_rows:,}")
    print(f"Database: {args.db}")
    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
