"""Unified activity timeline for Salesforce Data Browser Keboola view.

Combines Task, Event and EmailMessage records related to a given parent
record into a single, date-sorted, grouped timeline.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from sf_browser.database import is_safe_ident, quote_ident, table_columns
from sf_browser.filters import _format_datetime


# --------------------------------------------------------------------------- #
# Configuration — which FK column on each activity table to use per parent.
# --------------------------------------------------------------------------- #

# Parent objects where Task/Event link via AccountId.
_ACCOUNT_LIKE = {"Account"}
# Parent objects where Task/Event link via WhoId (people).
_WHO_LIKE = {"Contact", "Lead"}
# Parent objects where Task/Event link via WhatId (everything else that has
# activities attached — opportunities, cases, orders, contracts, custom objs).
# Note: anything not explicitly Account/Contact/Lead falls back to WhatId.

# Hard cap on the merged timeline.
_DEFAULT_LIMIT = 200
# Past months shown individually before collapsing into "Older".
_INDIVIDUAL_PAST_MONTHS = 12


_GONG_IN_RE = re.compile(r"^\[Gong\s+In\]", re.IGNORECASE)
_GONG_OUT_RE = re.compile(r"^\[Gong\s+Out\]", re.IGNORECASE)
_GONG_BARE_RE = re.compile(r"^\[Gong\]", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Column selection helper
# --------------------------------------------------------------------------- #


def _activity_columns(parent_object: str) -> dict[str, str | None]:
    """Return the FK column to use on each activity table for ``parent_object``.

    Keys are "Task", "Event", "EmailMessage". A value of ``None`` means the
    table cannot plausibly match this parent, so skip the query entirely.
    """
    if parent_object in _ACCOUNT_LIKE:
        return {
            "Task": "AccountId",
            "Event": "AccountId",
            "EmailMessage": "RelatedToId",
        }
    if parent_object in _WHO_LIKE:
        return {
            "Task": "WhoId",
            "Event": "WhoId",
            "EmailMessage": "RelatedToId",
        }
    # Default for Opportunity / Order / Case / Contract / custom objects.
    return {
        "Task": "WhatId",
        "Event": "WhatId",
        "EmailMessage": "RelatedToId",
    }


# --------------------------------------------------------------------------- #
# Date parsing
# --------------------------------------------------------------------------- #


def _parse_any_datetime(raw: Any) -> datetime | None:
    """Robustly parse SF date strings (epoch ms or ISO 8601) to a datetime.

    Returns None for empty / unparseable values.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    # Epoch milliseconds (SF export sometimes stores this format).
    if text.isdigit() and len(text) >= 10:
        try:
            return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            pass

    # Plain date (YYYY-MM-DD).
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            # Ensure tz-aware for consistent comparisons.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (TypeError, ValueError):
            continue
    return None


def _format_item_date(dt: datetime | None, raw: str | None) -> str:
    """Format for display. Uses existing ``_format_datetime`` when available."""
    if raw:
        formatted = _format_datetime(str(raw))
        if formatted:
            return formatted
    if dt is not None:
        return dt.strftime("%Y-%m-%d %H:%M")
    return ""


# --------------------------------------------------------------------------- #
# Gong detection
# --------------------------------------------------------------------------- #


def _detect_gong(subject: str | None) -> tuple[bool, str | None]:
    """Inspect the subject prefix and classify the activity as a Gong call."""
    if not subject:
        return False, None
    text = str(subject).strip()
    if _GONG_IN_RE.match(text):
        return True, "in"
    if _GONG_OUT_RE.match(text):
        return True, "out"
    if _GONG_BARE_RE.match(text):
        return True, None
    return False, None


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #


def _safe_query(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple,
) -> list[sqlite3.Row]:
    """Run a read-only query, returning [] if the table/column is missing."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.DatabaseError:
        return []


def _has_table_with_columns(
    conn: sqlite3.Connection,
    table: str,
    required: list[str],
) -> bool:
    """True if the table exists and contains every required column."""
    cols = set(table_columns(conn, table))
    return bool(cols) and all(c in cols for c in required)


# --------------------------------------------------------------------------- #
# Per-table loaders
# --------------------------------------------------------------------------- #


def _load_tasks(
    conn: sqlite3.Connection,
    fk_column: str,
    record_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not is_safe_ident(fk_column):
        return []
    if not _has_table_with_columns(conn, "Task", [fk_column, "Id"]):
        return []
    available = set(table_columns(conn, "Task"))
    # Only request columns we actually know exist.
    wanted = ["Id", "Subject", "ActivityDate", "CreatedDate", "Status", "OwnerId", "Type"]
    cols = [c for c in wanted if c in available]
    select = ", ".join(quote_ident(c) for c in cols)
    sql = (
        f"SELECT {select} FROM \"Task\" "
        f"WHERE {quote_ident(fk_column)} = ? "
        f"ORDER BY COALESCE(\"ActivityDate\", \"CreatedDate\") DESC "
        f"LIMIT ?"
    )
    rows = _safe_query(conn, sql, (record_id, limit))
    items: list[dict[str, Any]] = []
    for row in rows:
        raw_primary = row["ActivityDate"] if "ActivityDate" in row.keys() else None
        raw_fallback = row["CreatedDate"] if "CreatedDate" in row.keys() else None
        dt = _parse_any_datetime(raw_primary) or _parse_any_datetime(raw_fallback)
        subject = row["Subject"] if "Subject" in row.keys() else None
        is_gong, gong_direction = _detect_gong(subject)
        items.append(
            {
                "type": "task",
                "id": row["Id"],
                "subject": subject or "(no subject)",
                "date": _format_item_date(dt, raw_primary or raw_fallback),
                "_dt": dt,
                "owner_id": row["OwnerId"] if "OwnerId" in row.keys() else None,
                "owner_name": None,
                "is_gong": is_gong,
                "gong_direction": gong_direction,
                "status": row["Status"] if "Status" in row.keys() else None,
                "extra": {
                    "activity_date_raw": raw_primary,
                    "type": row["Type"] if "Type" in row.keys() else None,
                },
            }
        )
    return items


def _load_events(
    conn: sqlite3.Connection,
    fk_column: str,
    record_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not is_safe_ident(fk_column):
        return []
    if not _has_table_with_columns(conn, "Event", [fk_column, "Id"]):
        return []
    available = set(table_columns(conn, "Event"))
    wanted = [
        "Id", "Subject", "ActivityDate", "StartDateTime", "EndDateTime",
        "DurationInMinutes", "CreatedDate", "OwnerId", "Type",
    ]
    cols = [c for c in wanted if c in available]
    select = ", ".join(quote_ident(c) for c in cols)
    # Prefer StartDateTime for sorting when present, fall back to ActivityDate then CreatedDate.
    order_parts = []
    for candidate in ("StartDateTime", "ActivityDate", "CreatedDate"):
        if candidate in available:
            order_parts.append(quote_ident(candidate))
    order_expr = f"COALESCE({', '.join(order_parts)})" if order_parts else quote_ident("Id")
    sql = (
        f"SELECT {select} FROM \"Event\" "
        f"WHERE {quote_ident(fk_column)} = ? "
        f"ORDER BY {order_expr} DESC "
        f"LIMIT ?"
    )
    rows = _safe_query(conn, sql, (record_id, limit))
    items: list[dict[str, Any]] = []
    for row in rows:
        keys = row.keys()
        start_raw = row["StartDateTime"] if "StartDateTime" in keys else None
        activity_raw = row["ActivityDate"] if "ActivityDate" in keys else None
        created_raw = row["CreatedDate"] if "CreatedDate" in keys else None
        dt = (
            _parse_any_datetime(start_raw)
            or _parse_any_datetime(activity_raw)
            or _parse_any_datetime(created_raw)
        )
        subject = row["Subject"] if "Subject" in keys else None
        is_gong, gong_direction = _detect_gong(subject)
        items.append(
            {
                "type": "event",
                "id": row["Id"],
                "subject": subject or "(no subject)",
                "date": _format_item_date(dt, start_raw or activity_raw or created_raw),
                "_dt": dt,
                "owner_id": row["OwnerId"] if "OwnerId" in keys else None,
                "owner_name": None,
                "is_gong": is_gong,
                "gong_direction": gong_direction,
                "status": None,
                "extra": {
                    "duration_minutes": row["DurationInMinutes"] if "DurationInMinutes" in keys else None,
                    "activity_date_raw": activity_raw,
                    "start_datetime_raw": start_raw,
                    "type": row["Type"] if "Type" in keys else None,
                },
            }
        )
    return items


def _load_emails(
    conn: sqlite3.Connection,
    fk_column: str,
    record_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not is_safe_ident(fk_column):
        return []
    if not _has_table_with_columns(conn, "EmailMessage", [fk_column, "Id"]):
        return []
    available = set(table_columns(conn, "EmailMessage"))
    wanted = [
        "Id", "Subject", "MessageDate", "CreatedDate", "Status",
        "FromAddress", "ToAddress", "Incoming", "CreatedById",
    ]
    cols = [c for c in wanted if c in available]
    select = ", ".join(quote_ident(c) for c in cols)
    sql = (
        f"SELECT {select} FROM \"EmailMessage\" "
        f"WHERE {quote_ident(fk_column)} = ? "
        f"ORDER BY COALESCE(\"MessageDate\", \"CreatedDate\") DESC "
        f"LIMIT ?"
    )
    rows = _safe_query(conn, sql, (record_id, limit))
    items: list[dict[str, Any]] = []
    for row in rows:
        keys = row.keys()
        msg_raw = row["MessageDate"] if "MessageDate" in keys else None
        created_raw = row["CreatedDate"] if "CreatedDate" in keys else None
        dt = _parse_any_datetime(msg_raw) or _parse_any_datetime(created_raw)
        subject = row["Subject"] if "Subject" in keys else None
        # Gong prefix can occasionally appear on emails (Gong records
        # transcripts as "[Gong]" entries).
        is_gong, gong_direction = _detect_gong(subject)
        # EmailMessage doesn't have OwnerId; use CreatedById so the timeline
        # still shows an "author" link when resolvable.
        owner_id = row["CreatedById"] if "CreatedById" in keys else None
        items.append(
            {
                "type": "email",
                "id": row["Id"],
                "subject": subject or "(no subject)",
                "date": _format_item_date(dt, msg_raw or created_raw),
                "_dt": dt,
                "owner_id": owner_id,
                "owner_name": None,
                "is_gong": is_gong,
                "gong_direction": gong_direction,
                "status": row["Status"] if "Status" in keys else None,
                "extra": {
                    "from": row["FromAddress"] if "FromAddress" in keys else None,
                    "to": row["ToAddress"] if "ToAddress" in keys else None,
                    "incoming": row["Incoming"] if "Incoming" in keys else None,
                    "message_date_raw": msg_raw,
                },
            }
        )
    return items


# --------------------------------------------------------------------------- #
# Owner resolution
# --------------------------------------------------------------------------- #


def _resolve_owner_names(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
) -> None:
    """Fill in ``owner_name`` in-place using a single batched User lookup."""
    ids: set[str] = set()
    for item in items:
        owner_id = item.get("owner_id")
        if owner_id and isinstance(owner_id, str):
            ids.add(owner_id)
    if not ids:
        return
    if not _has_table_with_columns(conn, "User", ["Id", "Name"]):
        return
    placeholders = ",".join("?" for _ in ids)
    rows = _safe_query(
        conn,
        f'SELECT "Id", "Name" FROM "User" WHERE "Id" IN ({placeholders})',
        tuple(ids),
    )
    lookup = {r["Id"]: r["Name"] for r in rows if r["Name"]}
    for item in items:
        owner_id = item.get("owner_id")
        if owner_id and owner_id in lookup:
            item["owner_name"] = lookup[owner_id]


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _months_between(a: date, b: date) -> int:
    """Integer month delta (a - b). Positive when a is later."""
    return (a.year - b.year) * 12 + (a.month - b.month)


def _group_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket timeline items by the rules described in the public docstring.

    Groups (in display order):
      - "Upcoming & Overdue" — future Task/Event or overdue open Task.
      - "This month" — items dated in the current calendar month.
      - "YYYY Month" — one bucket per past month, up to 12 months back.
      - "Older" — everything older than 12 full months ago.
    """
    today = datetime.now(tz=timezone.utc).date()
    current_month_key = (today.year, today.month)

    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []

    def _append(title: str, items_list: list[dict[str, Any]]) -> None:
        if not items_list:
            return
        if title not in groups:
            groups[title] = []
            order.append(title)
        groups[title].extend(items_list)

    upcoming: list[dict[str, Any]] = []
    monthly: dict[tuple[int, int], list[dict[str, Any]]] = {}
    older: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []

    for item in items:
        dt: datetime | None = item.get("_dt")
        if dt is None:
            undated.append(item)
            continue
        item_date = dt.date()

        # Upcoming/overdue logic applies to Task/Event only; emails are always
        # historical records of a sent/received message.
        if item["type"] in ("task", "event"):
            is_future = item_date >= today
            is_open_task = item["type"] == "task" and (item.get("status") or "").strip().lower() not in (
                "completed", "closed", "done",
            )
            if is_future or (item["type"] == "task" and item_date < today and is_open_task):
                # Future or overdue-open task.
                if is_future or (is_open_task and item_date < today):
                    upcoming.append(item)
                    continue

        month_key = (item_date.year, item_date.month)
        if month_key == current_month_key:
            monthly.setdefault(month_key, []).append(item)
            continue
        months_back = _months_between(today.replace(day=1), date(item_date.year, item_date.month, 1))
        if 0 < months_back <= _INDIVIDUAL_PAST_MONTHS:
            monthly.setdefault(month_key, []).append(item)
        else:
            older.append(item)

    # Render in required order.
    if upcoming:
        upcoming.sort(key=lambda it: it["_dt"] or datetime.min.replace(tzinfo=timezone.utc))
        # Future first (ascending), then overdue (most recent past first)?
        # Simpler: sort ascending by date so soonest-due sits at the top.
        _append("Upcoming & Overdue", upcoming)

    if current_month_key in monthly:
        monthly[current_month_key].sort(
            key=lambda it: it["_dt"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        _append("This month", monthly.pop(current_month_key))

    # Past months, newest first.
    for key in sorted(monthly.keys(), reverse=True):
        year, month = key
        label = f"{_MONTH_NAMES[month - 1]} {year}"
        monthly[key].sort(
            key=lambda it: it["_dt"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        _append(label, monthly[key])

    if older:
        older.sort(
            key=lambda it: it["_dt"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        _append("Older", older)

    if undated:
        # Show undated items last so they don't crowd out the chronological flow.
        _append("Undated", undated)

    # Build final list in the order they were appended, stripping the
    # transient ``_dt`` key to keep the payload JSON-friendly.
    out: list[dict[str, Any]] = []
    # Note: upcoming is stored as a list, so wrap in group like others above.
    # (already appended via _append)
    for title in order:
        cleaned_items = []
        for item in groups[title]:
            stripped = {k: v for k, v in item.items() if k != "_dt"}
            cleaned_items.append(stripped)
        out.append({"title": title, "items": cleaned_items})
    return out


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def build_timeline(
    conn: sqlite3.Connection,
    object_name: str,
    record_id: str,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return unified activity timeline for a record.

    The dict shape is stable regardless of how many items matched — callers
    can safely iterate ``result["groups"]`` even when empty. When the raw
    result would exceed ``limit`` items, the most recent ``limit`` items are
    kept and ``overflow`` + ``total_matches`` are set so the UI can mention it.
    """
    if not object_name or not record_id:
        return {"groups": [], "total": 0}

    columns = _activity_columns(object_name)
    # Load per-source items. Each loader already applies the ``limit`` so we
    # don't have to worry about a single source drowning the others.
    raw_items: list[dict[str, Any]] = []
    task_col = columns.get("Task")
    if task_col:
        raw_items.extend(_load_tasks(conn, task_col, record_id, limit))
    event_col = columns.get("Event")
    if event_col:
        raw_items.extend(_load_events(conn, event_col, record_id, limit))
    email_col = columns.get("EmailMessage")
    if email_col:
        raw_items.extend(_load_emails(conn, email_col, record_id, limit))

    if not raw_items:
        return {"groups": [], "total": 0}

    # Global sort by datetime desc (items without a date fall to the bottom).
    def _sort_key(item: dict[str, Any]) -> tuple[int, datetime]:
        dt = item.get("_dt")
        if dt is None:
            return (0, datetime.min.replace(tzinfo=timezone.utc))
        return (1, dt)

    raw_items.sort(key=_sort_key, reverse=True)

    total_matches = len(raw_items)
    overflow = total_matches > limit
    if overflow:
        raw_items = raw_items[:limit]

    # Resolve owner (Task/Event.OwnerId, EmailMessage.CreatedById) names in bulk.
    _resolve_owner_names(conn, raw_items)

    grouped = _group_items(raw_items)
    result: dict[str, Any] = {
        "groups": grouped,
        "total": sum(len(g["items"]) for g in grouped),
    }
    if overflow:
        result["overflow"] = True
        result["total_matches"] = total_matches
    return result
