"""Migration helpers for the Salesforce Data Browser.

Supports the main goal of the app: help the user move data OFF Salesforce.

Produces per-field statistics (fill rate, distinct count, top values),
suggested PostgreSQL DDL column types, full ``CREATE TABLE`` statements
and streaming CSV export for the selected subset of fields.

The functions here only READ from the database — they never modify it.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any, Iterable, Iterator

from sf_browser import database as db

# Skip column-scans for columns wider than this (textarea/long strings).
# Scanning COUNT(DISTINCT) or GROUP BY on 32k+ TEXT columns of a 100k row
# table is unacceptably slow without a functional index.
_WIDE_COLUMN_THRESHOLD = 4000

# Upper bound for stats row-sample count per field (defensive — in practice
# a single indexed pass over the table is used and then aggregated).
_TOP_VALUES_LIMIT = 5


# --------------------------------------------------------------------------- #
# Type mapping
# --------------------------------------------------------------------------- #


def suggested_target_type(
    sf_type: str | None,
    length: int | None,
    scale: int | None = None,
) -> str:
    """Map an SF field type + length to a PostgreSQL DDL column type.

    Returns an uppercase DDL type string such as ``VARCHAR(255)``,
    ``TEXT``, ``NUMERIC(18,2)`` etc. Unknown types fall back to ``TEXT``.
    """
    sf_type = (sf_type or "").lower()
    length = length or 0

    if sf_type in ("id", "reference"):
        return "VARCHAR(18)"
    if sf_type == "boolean":
        return "BOOLEAN"
    if sf_type == "datetime":
        return "TIMESTAMP WITH TIME ZONE"
    if sf_type == "date":
        return "DATE"
    if sf_type == "time":
        return "TIME"
    if sf_type == "int":
        return "INTEGER"
    if sf_type == "long":
        return "BIGINT"
    if sf_type in ("double", "currency", "percent"):
        return "NUMERIC(18,2)"
    if sf_type in ("email", "phone", "url"):
        return "VARCHAR(255)"
    if sf_type == "picklist":
        return "VARCHAR(100)"
    if sf_type == "multipicklist":
        return "VARCHAR(4000)"
    if sf_type in ("address", "location"):
        return "TEXT"
    if sf_type == "textarea":
        return "TEXT"
    if sf_type == "string":
        if length and length <= 255:
            return f"VARCHAR({length})"
        if length and length > 255:
            return "TEXT"
        return "VARCHAR(255)"
    if sf_type in ("base64", "anyType", "encryptedstring", "combobox"):
        return "TEXT"

    # Unknown → TEXT (DDL generator will annotate with a comment).
    return "TEXT"


# --------------------------------------------------------------------------- #
# Per-field statistics
# --------------------------------------------------------------------------- #


def _is_heavy_text_field(field: dict[str, Any]) -> bool:
    """True when a field is too wide to safely run DISTINCT / GROUP BY on."""
    length = field.get("length") or 0
    sf_type = (field.get("sf_type") or "").lower()
    if length > _WIDE_COLUMN_THRESHOLD:
        return True
    if sf_type == "textarea" and length > 1000:
        return True
    return False


def _fill_counts(
    conn: sqlite3.Connection,
    object_name: str,
    columns: list[str],
    total: int,
) -> dict[str, int]:
    """Compute non-null non-empty counts for every column in ``columns``.

    Uses a single scan with ``SUM(CASE WHEN ...)`` expressions so a
    100k-row / 200-column scan completes in well under 10 seconds.
    """
    if not columns or total == 0:
        return {c: 0 for c in columns}

    # Batch columns so we don't generate absurdly long SQL strings.
    batch_size = 100
    result: dict[str, int] = {}

    for i in range(0, len(columns), batch_size):
        chunk = columns[i : i + batch_size]
        # For TEXT fields SQLite stores '' for empty strings; for numeric
        # fields empty string conversion would throw, so we only compare
        # against '' when the column is text-ish. Using the generic
        # `IS NOT NULL AND CAST(... AS TEXT) != ''` keeps things portable.
        selects = ", ".join(
            f"SUM(CASE WHEN {db.quote_ident(c)} IS NOT NULL "
            f"AND CAST({db.quote_ident(c)} AS TEXT) != '' THEN 1 ELSE 0 END) "
            f"AS {db.quote_ident('f_' + c)}"
            for c in chunk
        )
        sql = f"SELECT {selects} FROM {db.quote_ident(object_name)}"
        row = conn.execute(sql).fetchone()
        for c in chunk:
            val = row["f_" + c]
            result[c] = int(val or 0)
    return result


def _distinct_and_top(
    conn: sqlite3.Connection,
    object_name: str,
    column: str,
    *,
    top_limit: int = _TOP_VALUES_LIMIT,
) -> tuple[int, list[tuple[str, int]]]:
    """Return (distinct_count, [(value, count), ...])."""
    q_obj = db.quote_ident(object_name)
    q_col = db.quote_ident(column)
    try:
        # Distinct count — a single aggregate scan.
        dc_row = conn.execute(
            f"SELECT COUNT(DISTINCT {q_col}) FROM {q_obj} "
            f"WHERE {q_col} IS NOT NULL AND CAST({q_col} AS TEXT) != ''"
        ).fetchone()
        distinct_count = int(dc_row[0] or 0)
        top_rows = conn.execute(
            f"SELECT {q_col} AS v, COUNT(*) AS c FROM {q_obj} "
            f"WHERE {q_col} IS NOT NULL AND CAST({q_col} AS TEXT) != '' "
            f"GROUP BY {q_col} ORDER BY c DESC, v ASC LIMIT ?",
            (top_limit,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return 0, []

    top_values: list[tuple[str, int]] = []
    for row in top_rows:
        raw = row["v"]
        if raw is None:
            continue
        text = str(raw)
        if len(text) > 80:
            text = text[:77] + "..."
        top_values.append((text, int(row["c"])))
    return distinct_count, top_values


def field_stats(conn: sqlite3.Connection, object_name: str) -> list[dict[str, Any]]:
    """Return per-field statistics for ``object_name``.

    Skips BLOB columns entirely. For very wide text columns the distinct /
    top-values queries are skipped (``distinct_count=None``, ``top_values=[]``)
    because COUNT(DISTINCT) on a 32k-wide TEXT column over a large table
    triggers an unacceptable full scan with large sort buffers.
    """
    if not db.is_safe_ident(object_name):
        raise ValueError(f"invalid object name: {object_name}")

    fields = db.list_fields(conn, object_name)
    table_cols = set(db.table_columns(conn, object_name))

    # Total row count — computed once.
    try:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {db.quote_ident(object_name)}"
            ).fetchone()[0]
        )
    except sqlite3.DatabaseError:
        total = 0

    # Keep only fields that actually exist in the sqlite table and aren't BLOBs.
    usable_fields = []
    for f in fields:
        if f["field_name"] not in table_cols:
            continue
        if (f.get("sqlite_type") or "").upper() == "BLOB":
            continue
        usable_fields.append(f)

    fill_counts = _fill_counts(
        conn,
        object_name,
        [f["field_name"] for f in usable_fields],
        total,
    )

    results: list[dict[str, Any]] = []
    for f in usable_fields:
        col = f["field_name"]
        fill = int(fill_counts.get(col, 0) or 0)
        fill_rate = (fill / total) if total else 0.0

        heavy = _is_heavy_text_field(f)
        if fill == 0 or heavy:
            distinct_count: int | None = 0 if fill == 0 else None
            top_values: list[tuple[str, int]] = []
        else:
            distinct_count, top_values = _distinct_and_top(
                conn, object_name, col
            )

        results.append(
            {
                "field_name": col,
                "label": f.get("label") or col,
                "sf_type": f.get("sf_type"),
                "sqlite_type": f.get("sqlite_type"),
                "length": f.get("length"),
                "custom": bool(f.get("custom")),
                "nillable": bool(f.get("nillable")),
                "fill_count": fill,
                "fill_rate": round(fill_rate, 4),
                "distinct_count": distinct_count,
                "top_values": top_values,
                "suggested_type": suggested_target_type(
                    f.get("sf_type"), f.get("length")
                ),
            }
        )
    return results


# --------------------------------------------------------------------------- #
# DDL generation
# --------------------------------------------------------------------------- #


def _pg_quote(name: str) -> str:
    """Quote an identifier for PostgreSQL (double-quotes, quote-doubling)."""
    return '"' + name.replace('"', '""') + '"'


def generate_ddl(
    object_name: str,
    fields: list[dict[str, Any]],
    *,
    dialect: str = "postgres",
    exported_at: str | None = None,
) -> str:
    """Produce a ``CREATE TABLE`` statement for the selected fields.

    ``fields`` is the output of :func:`field_stats` filtered to the user
    selection. The fields are emitted in the given order. ``Id`` (if
    present) gets a ``PRIMARY KEY`` constraint, and non-nillable fields
    get ``NOT NULL``. Unknown SF types get an inline comment so the
    reader knows to review them.
    """
    if dialect != "postgres":
        raise ValueError(f"unsupported dialect: {dialect}")

    header_lines = [
        f"-- CREATE TABLE for Salesforce object: {object_name}",
    ]
    if exported_at:
        header_lines.append(f"-- Source export: {exported_at}")
    header_lines.append(f"-- Fields: {len(fields)}")
    header = "\n".join(header_lines)

    columns: list[str] = []
    has_id = any(f["field_name"] == "Id" for f in fields)

    for f in fields:
        col = f["field_name"]
        sf_type = f.get("sf_type")
        length = f.get("length")
        nillable = f.get("nillable", True)
        ddl_type = suggested_target_type(sf_type, length)

        pieces = [f"    {_pg_quote(col)} {ddl_type}"]
        if col == "Id" and has_id:
            pieces.append("PRIMARY KEY")
        elif not nillable:
            pieces.append("NOT NULL")

        line = " ".join(pieces)
        # Annotate unknown/fallback types so a human reviewer knows to check.
        if sf_type and sf_type.lower() not in {
            "id", "reference", "boolean", "datetime", "date", "time",
            "int", "long", "double", "currency", "percent", "email",
            "phone", "url", "picklist", "multipicklist", "address",
            "location", "textarea", "string", "base64", "anytype",
            "encryptedstring", "combobox",
        }:
            line += f"  -- unknown sf_type: {sf_type}"
        columns.append(line)

    body = ",\n".join(columns) if columns else "    -- no fields selected"
    table = _pg_quote(object_name)
    return f"{header}\nCREATE TABLE {table} (\n{body}\n);\n"


# --------------------------------------------------------------------------- #
# CSV streaming export
# --------------------------------------------------------------------------- #


def export_csv_stream(
    conn: sqlite3.Connection,
    object_name: str,
    field_names: list[str],
    *,
    chunk_size: int = 1000,
) -> Iterator[str]:
    """Return an iterator of CSV chunks (header + rows) for ``object_name``.

    The supplied ``conn`` is used only synchronously during setup: the column
    list is validated and the header/warning rows are pre-built so any error
    surfaces before streaming begins. The actual row streaming uses a fresh
    read-only SQLite connection because Flask closes the request-scoped
    connection on teardown, which happens *before* the WSGI server iterates
    the generator body.
    """
    if not db.is_safe_ident(object_name):
        raise ValueError(f"invalid object name: {object_name}")

    table_cols = db.table_columns(conn, object_name)
    table_col_set = set(table_cols)

    # Preserve user order but drop unknown columns.
    selected = [c for c in field_names if c in table_col_set]
    dropped = [c for c in field_names if c not in table_col_set]

    prelude_chunks: list[str] = []
    if dropped:
        # CSV doesn't officially support comments, but a leading '#' line is
        # the convention. Most tools (pandas, duckdb, psql \copy) can skip
        # '#'-prefixed lines.
        prelude_chunks.append(f"# dropped unknown columns: {', '.join(dropped)}\n")

    if not selected:
        # Empty header row so the response is still valid-ish CSV.
        buf = io.StringIO()
        csv.writer(buf).writerow([])
        prelude_chunks.append(buf.getvalue())
        return iter(prelude_chunks)

    header_buf = io.StringIO()
    csv.writer(header_buf).writerow(selected)
    prelude_chunks.append(header_buf.getvalue())

    # Defer the row streaming to a nested generator so exceptions during
    # setup happen eagerly (before the Flask teardown closes ``conn``).
    def _rows() -> Iterator[str]:
        uri = f"file:{db.DB_PATH}?mode=ro"
        stream_conn = sqlite3.connect(uri, uri=True)
        stream_conn.row_factory = sqlite3.Row
        try:
            col_list = ", ".join(db.quote_ident(c) for c in selected)
            sql = f"SELECT {col_list} FROM {db.quote_ident(object_name)}"
            cursor = stream_conn.execute(sql)
            while True:
                batch = cursor.fetchmany(chunk_size)
                if not batch:
                    break
                buf = io.StringIO()
                writer = csv.writer(buf)
                for row in batch:
                    writer.writerow([row[c] for c in selected])
                yield buf.getvalue()
        finally:
            stream_conn.close()

    def _chain() -> Iterator[str]:
        yield from prelude_chunks
        yield from _rows()

    return _chain()


__all__ = [
    "suggested_target_type",
    "field_stats",
    "generate_ddl",
    "export_csv_stream",
]
