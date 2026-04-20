"""SQLite connection helpers for the Salesforce Data Browser."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from flask import g

# ``sf_browser/`` is a sibling of ``data/`` — walk up one level to the repo root.
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

DB_PATH = Path(
    os.environ.get("SF_FULL_DB", str(DATA_DIR / "salesforce_full.db"))
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def quote_ident(name: str) -> str:
    """Quote a SQLite identifier with double quotes, escaping embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def is_safe_ident(name: str) -> bool:
    """True if name looks like a plain SF field / table identifier."""
    return bool(_IDENT_RE.match(name or ""))


def get_connection() -> sqlite3.Connection:
    """Return a per-request SQLite connection opened read-only."""
    if "db" not in g:
        if not DB_PATH.exists():
            raise FileNotFoundError(
                f"Salesforce full-export DB not found at {DB_PATH}. "
                "Run `make refresh` first."
            )
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def list_objects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all exported SF objects with their record counts."""
    rows = conn.execute(
        "SELECT name, label, custom, record_count, exported_at FROM _sf_objects ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def get_object(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT name, label, custom, record_count, exported_at, key_prefix "
        "FROM _sf_objects WHERE name = ?",
        (name,),
    ).fetchone()
    return dict(row) if row else None


def list_fields(conn: sqlite3.Connection, object_name: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT field_name, label, sf_type, sqlite_type, length, reference_to, "
        "       relationship_name, custom, nillable "
        "FROM _sf_fields WHERE object_name = ? ORDER BY field_name",
        (object_name,),
    ).fetchall()
    return [dict(r) for r in rows]


def inbound_relationships(conn: sqlite3.Connection, object_name: str) -> list[dict[str, Any]]:
    """Return objects/fields that reference ``object_name``."""
    rows = conn.execute(
        "SELECT from_object, from_field, relationship_name FROM _sf_relationships "
        "WHERE to_object = ? ORDER BY from_object, from_field",
        (object_name,),
    ).fetchall()
    return [dict(r) for r in rows]


def outbound_relationships(conn: sqlite3.Connection, object_name: str) -> list[dict[str, Any]]:
    """Return reference fields on ``object_name`` (which point elsewhere)."""
    rows = conn.execute(
        "SELECT from_field, to_object, relationship_name FROM _sf_relationships "
        "WHERE from_object = ? ORDER BY from_field",
        (object_name,),
    ).fetchall()
    return [dict(r) for r in rows]


def distinct_values(
    conn: sqlite3.Connection,
    object_name: str,
    column: str,
    *,
    limit: int = 50,
) -> list[str]:
    """Return distinct non-null values for a column, ordered by frequency."""
    if not is_safe_ident(object_name) or not is_safe_ident(column):
        return []
    try:
        rows = conn.execute(
            f"SELECT {quote_ident(column)}, COUNT(*) as cnt "
            f"FROM {quote_ident(object_name)} "
            f"WHERE {quote_ident(column)} IS NOT NULL AND {quote_ident(column)} != '' "
            f"GROUP BY {quote_ident(column)} ORDER BY cnt DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [str(r[0]) for r in rows]


_NAME_COLUMN_CANDIDATES = ("Name", "Subject", "Title", "CaseNumber", "OrderNumber")


def _pick_name_column(conn: sqlite3.Connection, object_name: str) -> str | None:
    """Return the first available human-readable identifier column."""
    cols = set(table_columns(conn, object_name))
    for candidate in _NAME_COLUMN_CANDIDATES:
        if candidate in cols:
            return candidate
    return None


def distinct_reference_names(
    conn: sqlite3.Connection,
    object_name: str,
    column: str,
    target_object: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Distinct reference values on ``object.column`` joined to target's name.

    Returns list of ``{id, label, count}``. ``label`` falls back to ``id`` when
    the target record isn't in this export.
    """
    if not (
        is_safe_ident(object_name)
        and is_safe_ident(column)
        and is_safe_ident(target_object)
    ):
        return []
    name_col = _pick_name_column(conn, target_object)
    if not name_col:
        # Target has no friendly name — fall back to plain distinct.
        return [
            {"id": v, "label": v, "count": 0} for v in distinct_values(conn, object_name, column, limit=limit)
        ]
    try:
        sql = (
            f"SELECT src.{quote_ident(column)} AS id, "
            f"       tgt.{quote_ident(name_col)} AS label, "
            f"       COUNT(*) AS cnt "
            f"FROM {quote_ident(object_name)} src "
            f"LEFT JOIN {quote_ident(target_object)} tgt "
            f'  ON src.{quote_ident(column)} = tgt."Id" '
            f"WHERE src.{quote_ident(column)} IS NOT NULL "
            f"  AND src.{quote_ident(column)} != '' "
            f"GROUP BY src.{quote_ident(column)}, tgt.{quote_ident(name_col)} "
            f"ORDER BY cnt DESC LIMIT ?"
        )
        rows = conn.execute(sql, (limit,)).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [
        {"id": r["id"], "label": r["label"] or r["id"], "count": r["cnt"]}
        for r in rows
    ]


def resolve_name_to_ids(
    conn: sqlite3.Connection,
    target_object: str,
    name_query: str,
    *,
    limit: int = 2000,
) -> list[str]:
    """Return IDs in ``target_object`` whose name column matches ``name_query``.

    Uses LIKE '%...%' against the first available name column. Empty query
    returns an empty list.
    """
    if not name_query or not is_safe_ident(target_object):
        return []
    name_col = _pick_name_column(conn, target_object)
    if not name_col:
        return []
    try:
        rows = conn.execute(
            f'SELECT "Id" FROM {quote_ident(target_object)} '
            f"WHERE {quote_ident(name_col)} LIKE ? LIMIT ?",
            (f"%{name_query}%", limit),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [r["Id"] for r in rows]


def table_columns(conn: sqlite3.Connection, object_name: str) -> list[str]:
    """Return actual column names of the stored table for an object."""
    if not is_safe_ident(object_name):
        return []
    try:
        rows = conn.execute(
            f"PRAGMA table_info({quote_ident(object_name)})"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [r["name"] for r in rows]


def fetch_records(
    conn: sqlite3.Connection,
    object_name: str,
    *,
    page: int = 1,
    page_size: int = 50,
    sort: str | None = None,
    sort_dir: str = "asc",
    filters: dict[str, Any] | None = None,
) -> tuple[list[sqlite3.Row], int]:
    """Paginated/sorted/filtered fetch over a single object's table.

    Filter value semantics:
      - ``str``       → column LIKE '%val%' (substring match).
      - ``list[str]`` → column IN (v1, v2, ...). Empty list forces zero results.
    """
    if not is_safe_ident(object_name):
        raise ValueError("invalid object name")

    valid_cols = set(table_columns(conn, object_name))
    where_clauses: list[str] = []
    params: list[Any] = []
    if filters:
        for col, val in filters.items():
            if col not in valid_cols:
                continue
            if isinstance(val, list):
                if not val:
                    # Empty list = no match → force zero results.
                    where_clauses.append("1=0")
                    continue
                placeholders = ",".join("?" for _ in val)
                where_clauses.append(f"{quote_ident(col)} IN ({placeholders})")
                params.extend(val)
            elif val:
                where_clauses.append(f"{quote_ident(col)} LIKE ?")
                params.append(f"%{val}%")
    where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    order = ""
    if sort and sort in valid_cols:
        direction = "DESC" if sort_dir.lower() == "desc" else "ASC"
        order = f"ORDER BY {quote_ident(sort)} {direction}"

    offset = max((page - 1) * page_size, 0)
    count_sql = f"SELECT COUNT(*) FROM {quote_ident(object_name)} {where}"
    total = conn.execute(count_sql, params).fetchone()[0]
    data_sql = (
        f"SELECT * FROM {quote_ident(object_name)} {where} {order} "
        f"LIMIT ? OFFSET ?"
    )
    rows = conn.execute(data_sql, (*params, page_size, offset)).fetchall()
    return rows, total


def get_record(
    conn: sqlite3.Connection,
    object_name: str,
    record_id: str,
) -> dict[str, Any] | None:
    if not is_safe_ident(object_name):
        return None
    cols = table_columns(conn, object_name)
    if "Id" not in cols:
        return None
    row = conn.execute(
        f'SELECT * FROM {quote_ident(object_name)} WHERE "Id" = ? LIMIT 1',
        (record_id,),
    ).fetchone()
    return dict(row) if row else None


def related_records(
    conn: sqlite3.Connection,
    parent_object: str,
    parent_id: str,
    *,
    limit_per_child: int = 25,
) -> list[dict[str, Any]]:
    """For a given parent record, return counts + previews of every child list."""
    inbound = inbound_relationships(conn, parent_object)
    results: list[dict[str, Any]] = []
    for rel in inbound:
        child = rel["from_object"]
        field = rel["from_field"]
        if not is_safe_ident(child) or not is_safe_ident(field):
            continue
        child_cols = set(table_columns(conn, child))
        if field not in child_cols:
            # Child table might not have been imported (or column missing).
            continue
        try:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {quote_ident(child)} "
                f"WHERE {quote_ident(field)} = ?",
                (parent_id,),
            ).fetchone()[0]
            if count == 0:
                continue
            rows = conn.execute(
                f"SELECT * FROM {quote_ident(child)} "
                f"WHERE {quote_ident(field)} = ? LIMIT ?",
                (parent_id, limit_per_child),
            ).fetchall()
        except sqlite3.DatabaseError:
            continue
        results.append(
            {
                "object": child,
                "field": field,
                "relationship_name": rel.get("relationship_name"),
                "count": count,
                "preview": [dict(r) for r in rows],
            }
        )
    return results


def build_prefix_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Build a mapping from Salesforce key_prefix to object API name.

    Used to resolve polymorphic reference fields by inspecting the first
    3 characters of a Salesforce ID.
    """
    rows = conn.execute(
        "SELECT key_prefix, name FROM _sf_objects WHERE key_prefix IS NOT NULL AND key_prefix != ''"
    ).fetchall()
    return {r["key_prefix"]: r["name"] for r in rows}


def resolve_object_for_id(prefix_map: dict[str, str], sf_id: str) -> str | None:
    """Given a Salesforce 18-char ID, return the API object name via key_prefix."""
    if not sf_id or len(sf_id) < 3:
        return None
    return prefix_map.get(sf_id[:3])


def resolve_record_name(
    conn: sqlite3.Connection,
    object_name: str,
    record_id: str,
) -> str | None:
    """Look up the Name column for a single record. Returns None if not found."""
    if not is_safe_ident(object_name):
        return None
    cols = table_columns(conn, object_name)
    if "Name" not in cols:
        return None
    try:
        row = conn.execute(
            f'SELECT "Name" FROM {quote_ident(object_name)} WHERE "Id" = ? LIMIT 1',
            (record_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row["Name"] if row else None


def batch_resolve_names(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    fields: list[dict[str, Any]],
    prefix_map: dict[str, str],
) -> dict[str, tuple[str, str]]:
    """For every reference field in *record*, resolve the target object + Name.

    Returns ``{field_name: (object_name, display_name)}`` for fields that
    could be resolved.
    """
    resolved: dict[str, tuple[str, str]] = {}
    for f in fields:
        if f["sf_type"] != "reference":
            continue
        val = record.get(f["field_name"])
        if not val or not isinstance(val, str) or len(val) < 3:
            continue
        obj = resolve_object_for_id(prefix_map, val)
        if not obj:
            continue
        name = resolve_record_name(conn, obj, val)
        if name:
            resolved[f["field_name"]] = (obj, name)
        else:
            resolved[f["field_name"]] = (obj, "")
    return resolved


def get_export_metadata(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return export metadata (date, instance, totals) from _sf_objects."""
    try:
        row = conn.execute(
            "SELECT MIN(exported_at) as exported_at FROM _sf_objects"
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) as c FROM _sf_objects").fetchone()
        total_recs = conn.execute(
            "SELECT SUM(record_count) as s FROM _sf_objects"
        ).fetchone()
        return {
            "exported_at": row["exported_at"] if row else None,
            "total_objects": total["c"] if total else 0,
            "total_records": total_recs["s"] if total_recs else 0,
        }
    except sqlite3.DatabaseError:
        return {}


def _fts_column_names(conn: sqlite3.Connection, fts_table: str) -> list[str]:
    """Return the column names of an FTS5 virtual table (excluding sf_id)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({quote_ident(fts_table)})").fetchall()
    except sqlite3.DatabaseError:
        return []
    return [r["name"] for r in rows if r["name"] != "sf_id"]


def global_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit_per_object: int = 10,
) -> list[dict[str, Any]]:
    """Search all FTS indexes, returning matching records with snippets."""
    if not query:
        return []
    # Find all FTS virtual tables we created during import.
    fts_rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '_fts_%'"
    ).fetchall()
    # Filter out internal FTS shadow tables.
    fts_tables = []
    for row in fts_rows:
        name = row["name"]
        if any(name.endswith(suf) for suf in ("_data", "_config", "_content", "_docsize", "_idx")):
            continue
        fts_tables.append(name)

    results: list[dict[str, Any]] = []
    for fts_name in fts_tables:
        obj = fts_name[len("_fts_"):]
        if not is_safe_ident(obj):
            continue

        fts_cols = _fts_column_names(conn, fts_name)
        if not fts_cols:
            continue

        # Build snippet selects for each indexed column.
        # snippet(table, col_idx, open, close, ellipsis, max_tokens)
        # col_idx 0 = sf_id (UNINDEXED), so real columns start at 1.
        snippet_parts = []
        for i, _col in enumerate(fts_cols):
            snippet_parts.append(
                f"snippet({quote_ident(fts_name)}, {i + 1}, '<mark>', '</mark>', '…', 24) AS snip_{i}"
            )
        snippet_sql = ", ".join(snippet_parts)

        try:
            hits = conn.execute(
                f"SELECT sf_id, {snippet_sql} FROM {quote_ident(fts_name)} "
                f"WHERE {quote_ident(fts_name)} MATCH ? LIMIT ?",
                (query, limit_per_object),
            ).fetchall()
        except sqlite3.DatabaseError:
            continue
        if not hits:
            continue

        ids = [h["sf_id"] for h in hits]
        # Build snippet map: sf_id -> best snippet (first one containing <mark>).
        snippet_map: dict[str, tuple[str, str]] = {}  # id -> (column_name, snippet)
        for h in hits:
            for i, col in enumerate(fts_cols):
                snip = h[f"snip_{i}"]
                if snip and "<mark>" in snip:
                    snippet_map[h["sf_id"]] = (col, snip)
                    break
            else:
                # No highlighted snippet found; take first non-empty snippet.
                for i, col in enumerate(fts_cols):
                    snip = h[f"snip_{i}"]
                    if snip and snip.strip():
                        snippet_map[h["sf_id"]] = (col, snip)
                        break

        placeholders = ",".join("?" for _ in ids)
        cols = table_columns(conn, obj)
        if "Id" not in cols:
            continue
        rows = conn.execute(
            f'SELECT * FROM {quote_ident(obj)} WHERE "Id" IN ({placeholders})',
            ids,
        ).fetchall()

        records_with_snippets = []
        for r in rows:
            rec = dict(r)
            sid = rec.get("Id", "")
            snip_info = snippet_map.get(sid)
            if snip_info:
                rec["_snippet_col"] = snip_info[0]
                rec["_snippet"] = snip_info[1]
            records_with_snippets.append(rec)

        results.append(
            {
                "object": obj,
                "count": len(records_with_snippets),
                "records": records_with_snippets,
            }
        )
    return results


def smart_columns(
    all_cols: list[str],
    fields: list[dict[str, Any]],
    *,
    max_cols: int = 12,
) -> list[str]:
    """Pick the most useful columns for a table view.

    Priority:
    1. Id (always)
    2. Name / Subject / OrderNumber (human identifier)
    3. Key reference fields (Account, Contact, Owner, Opportunity)
    4. Status / Stage / Type / Priority
    5. Date fields (EffectiveDate, ActivityDate, CreatedDate)
    6. Amount / currency fields
    7. Remaining string/picklist fields
    """
    col_set = set(all_cols)
    field_map = {f["field_name"]: f for f in fields}
    chosen: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name in col_set and name not in seen:
            chosen.append(name)
            seen.add(name)

    # 1. Always Id
    _add("Id")

    # 2. Human-readable identifiers
    for name in ("Name", "Subject", "OrderNumber", "CaseNumber", "Title", "Email"):
        _add(name)

    # 3. Key reference fields
    for name in ("AccountId", "Account__c", "ContactId", "OwnerId",
                 "OpportunityId", "WhoId", "WhatId", "RelatedToId",
                 "Order__c", "ParentId", "EventId", "RelationId",
                 "EmailMessageId", "CampaignId", "ContractId",
                 "LeadId", "CaseId", "QuoteId", "Pricebook2Id",
                 "BillToContactId", "CreatedById"):
        _add(name)

    # 4. Status / classification
    for name in ("Status", "Status__c", "StageName", "Type", "Priority",
                 "RecordTypeId", "CurrencyIsoCode", "Invoicing_Currency__c"):
        _add(name)

    # 5. Dates
    for name in ("EffectiveDate", "EndDate", "ActivityDate", "Due_Date__c",
                 "CloseDate", "MessageDate", "CreatedDate"):
        _add(name)

    # 6. Amounts
    for name in ("Amount", "TotalAmount", "Total_Amount__c", "Total__c",
                 "ARR_Amount__c", "AnnualRevenue"):
        _add(name)

    # 7. Any remaining reference fields not yet included (important for
    #    junction/relation objects where ALL columns are references).
    if len(chosen) < max_cols:
        for col in all_cols:
            if col in seen:
                continue
            fm = field_map.get(col)
            if fm and fm["sf_type"] == "reference":
                _add(col)
                if len(chosen) >= max_cols:
                    break

    # Fill remaining with non-system string/picklist fields.
    if len(chosen) < max_cols:
        system_fields = {
            "IsDeleted", "SystemModstamp", "LastViewedDate", "LastReferencedDate",
            "LastModifiedById", "LastModifiedDate",
            "BillingLatitude", "BillingLongitude", "BillingGeocodeAccuracy",
            "ShippingLatitude", "ShippingLongitude", "ShippingGeocodeAccuracy",
        }
        for col in all_cols:
            if col in seen or col in system_fields:
                continue
            fm = field_map.get(col)
            if fm and fm["sf_type"] in ("string", "picklist"):
                _add(col)
                if len(chosen) >= max_cols:
                    break

    return chosen[:max_cols]


def batch_resolve_table_names(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    display_cols: list[str],
    fields: list[dict[str, Any]],
    prefix_map: dict[str, str],
) -> dict[str, str]:
    """Resolve names for reference IDs across all rows in a table page.

    Returns ``{salesforce_id: display_name}`` for every resolvable ID
    that appears in reference columns of the displayed rows.
    """
    field_map = {f["field_name"]: f for f in fields}
    ref_cols = [c for c in display_cols if field_map.get(c, {}).get("sf_type") == "reference"]

    # Collect all unique IDs per target object.
    ids_by_object: dict[str, set[str]] = {}
    for row in rows:
        for col in ref_cols:
            val = row.get(col)
            if not val or not isinstance(val, str) or len(val) < 3:
                continue
            obj = resolve_object_for_id(prefix_map, val)
            if obj:
                ids_by_object.setdefault(obj, set()).add(val)

    # Batch query Name for each target object.
    result: dict[str, str] = {}
    for obj, ids in ids_by_object.items():
        if not is_safe_ident(obj):
            continue
        cols = table_columns(conn, obj)
        # Use Name, or Subject, or Title as fallback.
        name_col = None
        for candidate in ("Name", "Subject", "Title"):
            if candidate in cols:
                name_col = candidate
                break
        if not name_col:
            continue
        id_list = list(ids)
        placeholders = ",".join("?" for _ in id_list)
        try:
            rows_res = conn.execute(
                f'SELECT "Id", {quote_ident(name_col)} FROM {quote_ident(obj)} '
                f'WHERE "Id" IN ({placeholders})',
                id_list,
            ).fetchall()
            for r in rows_res:
                if r[name_col]:
                    result[r["Id"]] = r[name_col]
        except sqlite3.DatabaseError:
            continue
    return result


def execute_readonly_query(
    conn: sqlite3.Connection,
    sql: str,
    *,
    max_rows: int = 500,
) -> dict[str, Any]:
    """Execute a single read-only SELECT. Returns {columns, rows, truncated}."""
    statements = [s for s in sql.split(";") if s.strip()]
    if len(statements) != 1:
        raise ValueError("Only a single SELECT statement is allowed.")
    stmt = statements[0].strip()
    lower = stmt.lower().lstrip()
    if not (lower.startswith("select") or lower.startswith("with") or lower.startswith("pragma")):
        raise ValueError("Only SELECT / WITH / PRAGMA statements are allowed.")

    cursor = conn.execute(stmt)
    columns = [d[0] for d in cursor.description] if cursor.description else []
    rows = cursor.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }
