"""Field-level audit timeline for Salesforce records.

Reads ``*History`` / ``*__History`` tables that follow the standard
``Field / OldValue / NewValue`` triplet shape, joins ``CreatedById`` to
``User.Name`` and the ``Field`` API name to its human label via
``_sf_fields``. Returns a date-sorted list ready for template rendering.

Why a dedicated module:
- The generic ``related_records`` view dumps history rows in a tabular
  preview that's hard to read (raw API field names, no user resolution).
- A timeline rendering ("on <date> <user> changed <field> from X to Y")
  matches the Lightning Audit Trail UX and is the primary use-case
  during a Salesforce off-boarding / forensics review.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from sf_browser.database import is_safe_ident, quote_ident, table_columns
from sf_browser.filters import _format_datetime

# Match standard ``XxxHistory`` and numbered variants (``CaseHistory2``);
# also ``__History`` for custom objects. Excludes ``__c`` custom-object
# names that happen to contain the word ``History`` (e.g.
# ``Employee_History__c`` is a regular business object, not an audit table).
_HISTORY_NAME_RE = re.compile(r".*History\d*$")


def is_history_table_name(name: str) -> bool:
    """Heuristic: does this object name look like a Salesforce history table?

    Catches:
    - ``AccountHistory``, ``OpportunityFieldHistory`` (standard)
    - ``CaseHistory2`` (re-bucketed standard)
    - ``Invoice__History`` (custom-object field history)

    Excludes ``__c`` custom objects whose name happens to contain "History".
    """
    if not name:
        return False
    if name.endswith("__c"):
        return False
    if name.endswith("__History"):
        return True
    return bool(_HISTORY_NAME_RE.match(name))

# A table is considered a generic field-history table when it carries the
# full Field/OldValue/NewValue triplet plus audit columns. Tables like
# ``OpportunityHistory`` (stage-snapshot shape) are deliberately excluded —
# they need a different rendering and remain available via related lists.
_HISTORY_REQUIRED_COLS = frozenset(
    {"Field", "OldValue", "NewValue", "CreatedById", "CreatedDate"}
)

# Hard cap on entries per record — matches the activity timeline's default.
_DEFAULT_LIMIT = 300


def find_field_history_tables(
    conn: sqlite3.Connection,
    parent_object: str,
) -> list[tuple[str, str]]:
    """Return ``[(history_table, fk_column), ...]`` for the given parent.

    Walks ``_sf_relationships`` for inbound edges into ``parent_object``,
    keeps only tables whose name ends with ``History`` / ``__History`` and
    that have the full Field/OldValue/NewValue triplet.
    """
    if not is_safe_ident(parent_object):
        return []
    try:
        rows = conn.execute(
            "SELECT from_object, from_field FROM _sf_relationships "
            "WHERE to_object = ?",
            (parent_object,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []

    found: list[tuple[str, str]] = []
    for r in rows:
        history_table = r["from_object"]
        fk_column = r["from_field"]
        if not is_history_table_name(history_table):
            continue
        if not (is_safe_ident(history_table) and is_safe_ident(fk_column)):
            continue
        cols = set(table_columns(conn, history_table))
        if not _HISTORY_REQUIRED_COLS.issubset(cols):
            continue
        if fk_column not in cols:
            continue
        found.append((history_table, fk_column))
    return found


def _field_label_map(
    conn: sqlite3.Connection, parent_object: str
) -> dict[str, str]:
    """Return ``{api_name: label}`` for every field of ``parent_object``."""
    try:
        rows = conn.execute(
            "SELECT field_name, label FROM _sf_fields WHERE object_name = ?",
            (parent_object,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {r["field_name"]: r["label"] for r in rows if r["label"]}


def _resolve_user_names(
    conn: sqlite3.Connection, user_ids: set[str]
) -> dict[str, str]:
    """Batch-resolve User.Id -> User.Name. Empty dict on failure."""
    if not user_ids:
        return {}
    try:
        # Quick existence check on the User table — Salesforce orgs always
        # have it, but a partial export might not.
        if "User" not in {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM _sf_objects WHERE name = 'User'"
            ).fetchall()
        }:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        rows = conn.execute(
            f'SELECT "Id", "Name" FROM "User" WHERE "Id" IN ({placeholders})',
            list(user_ids),
        ).fetchall()
        return {r["Id"]: r["Name"] for r in rows if r["Name"]}
    except sqlite3.DatabaseError:
        return {}


def build_field_history(
    conn: sqlite3.Connection,
    parent_object: str,
    parent_id: str,
    *,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Return a date-desc list of field changes for one record.

    Each entry: ``{id, date_raw, date_formatted, user_id, user_name,
    field_api, field_label, data_type, old_value, new_value, source}``.

    Multiple history sources may exist (e.g. an object that was renamed,
    or a polymorphic ParentId). They are merged and re-sorted by
    ``CreatedDate`` since SQL ``UNION`` ordering isn't guaranteed across
    different SQLite tables with potentially different row counts.
    """
    sources = find_field_history_tables(conn, parent_object)
    if not sources:
        return []

    label_map = _field_label_map(conn, parent_object)

    entries: list[dict[str, Any]] = []
    user_ids: set[str] = set()
    # Per-source query so we can use the FK column directly. UNION across
    # tables would require an alias and is harder to read.
    per_source = max(1, limit // max(1, len(sources)) + 50)
    for table, fk in sources:
        try:
            rows = conn.execute(
                f'SELECT "Id", "CreatedById", "CreatedDate", '
                f'  "Field", "DataType", "OldValue", "NewValue" '
                f"FROM {quote_ident(table)} "
                f"WHERE {quote_ident(fk)} = ? "
                f'ORDER BY "CreatedDate" DESC LIMIT ?',
                (parent_id, per_source),
            ).fetchall()
        except sqlite3.DatabaseError:
            continue
        for r in rows:
            uid = r["CreatedById"]
            if uid:
                user_ids.add(uid)
            raw_date = r["CreatedDate"]
            entries.append(
                {
                    "id": r["Id"],
                    "date_raw": raw_date,
                    "date_formatted": (
                        _format_datetime(str(raw_date)) if raw_date else ""
                    ),
                    "user_id": uid,
                    "user_name": None,
                    "field_api": r["Field"],
                    "field_label": label_map.get(r["Field"], r["Field"]),
                    "data_type": r["DataType"],
                    "old_value": r["OldValue"],
                    "new_value": r["NewValue"],
                    "source": table,
                }
            )

    name_by_id = _resolve_user_names(conn, user_ids)
    for e in entries:
        if e["user_id"]:
            e["user_name"] = name_by_id.get(e["user_id"])

    entries.sort(key=lambda e: str(e["date_raw"] or ""), reverse=True)
    return entries[:limit]
