"""Flask routes for the Salesforce Data Browser."""

from __future__ import annotations

import csv
import io
from typing import Any

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    url_for,
)

from sf_browser import activity
from sf_browser import database as db
from sf_browser import field_history
from sf_browser import files as sf_files
from sf_browser import layout
from sf_browser import migration
from sf_browser.filters import render_cell, render_keboola, salesforce_id

bp = Blueprint("sf", __name__)

# Salesforce objects that are most useful for forensics / debugging.
KEY_OBJECTS = [
    "Account", "Contact", "Lead", "Opportunity", "Order", "OrderItem",
    "Contract", "Case", "Task", "Event", "EmailMessage", "Campaign",
    "CampaignMember", "Product2", "Quote", "Invoice__c", "User",
]

# Fields to show at the top of record detail, in display order.
PRIORITY_FIELDS = [
    "Name", "Subject", "Email", "Title",
    "OwnerId", "AccountId", "ContactId", "OpportunityId",
    "RelatedToId", "WhoId", "WhatId", "ActivityId",
    "Status", "StageName", "Type", "RecordTypeId",
    "Amount", "CurrencyIsoCode",
    "CreatedById", "CreatedDate",
    "LastModifiedById", "LastModifiedDate",
]


# Human-readable descriptions for standard Salesforce objects.
# SF API describe() returns empty strings, so we maintain this curated dict.
SF_OBJECT_DESCRIPTIONS: dict[str, str] = {
    "Account": "Companies and organizations you do business with",
    "AccountContactRelation": "Links between accounts and contacts (many-to-many)",
    "AccountHistory": "Field change tracking for Account records",
    "AcceptedEventRelation": "Event invitees who accepted",
    "AIApplication": "Einstein AI application configurations",
    "AIInsightAction": "Actions suggested by Einstein AI insights",
    "AIInsightFeedback": "User feedback on Einstein AI predictions",
    "AIInsightReason": "Factors behind Einstein AI predictions",
    "AIInsightValue": "Predicted values from Einstein AI models",
    "AIRecordInsight": "Einstein AI predictions on individual records",
    "Attachment": "Files attached to records (classic attachments, pre-Lightning)",
    "Campaign": "Marketing campaigns for tracking leads and opportunities",
    "CampaignMember": "Contacts or leads associated with a campaign",
    "Case": "Customer support issues, questions, or feedback",
    "CaseHistory": "Field change tracking for Case records",
    "Contact": "People associated with your accounts",
    "ContactHistory": "Field change tracking for Contact records",
    "ContentDocument": "Files uploaded to Salesforce (Lightning file system)",
    "ContentDocumentLink": "Links between files and the records they're attached to",
    "ContentVersion": "Versions of files in Salesforce (metadata only, binary not exported)",
    "Contract": "Business agreements with accounts",
    "CurrencyType": "Active and inactive currencies in the org",
    "Dashboard": "Visual display of key metrics and trends",
    "DashboardComponent": "Individual charts/metrics within a dashboard",
    "DeclinedEventRelation": "Event invitees who declined",
    "Document": "Classic documents stored in document folders",
    "DuplicateRecordItem": "Individual records identified as duplicates",
    "DuplicateRecordSet": "Groups of records identified as duplicates",
    "EmailMessage": "Emails tracked in Salesforce (sent, received, or logged)",
    "EmailMessageRelation": "Links between emails and contacts/leads (from, to, cc, bcc)",
    "EmailTemplate": "Reusable email templates",
    "Event": "Calendar events and meetings",
    "EventRelation": "Event attendees and their responses",
    "Exchange_Rate__c": "Custom exchange rates for currency conversion",
    "FeedComment": "Comments on Chatter feed posts",
    "FeedItem": "Chatter feed posts (status updates, links, files)",
    "FieldPermissions": "Field-level security settings per permission set",
    "Group": "Public groups and queues for record sharing",
    "GroupMember": "Members of public groups",
    "Invoice__c": "Custom invoice records linked to orders and accounts",
    "Lead": "Potential customers not yet qualified as opportunities",
    "LeadHistory": "Field change tracking for Lead records",
    "LoginGeo": "Geographic location data for user logins",
    "Note": "Text notes attached to records",
    "ObjectPermissions": "Object-level CRUD permissions per profile/permission set",
    "Opportunity": "Sales deals being tracked through the pipeline",
    "OpportunityContactRole": "Contacts involved in an opportunity and their roles",
    "OpportunityHistory": "Stage changes and field tracking for opportunities",
    "OpportunityLineItem": "Products/services included in an opportunity",
    "Order": "Orders placed by customers (subscriptions, purchases)",
    "OrderItem": "Individual line items within an order",
    "PermissionSet": "Sets of permissions that extend user access",
    "PermissionSetAssignment": "Users assigned to permission sets",
    "Pricebook2": "Price books containing product prices",
    "PricebookEntry": "Product prices within a specific price book",
    "ProcessInstance": "Approval process instances (submitted records)",
    "ProcessInstanceStep": "Individual steps in an approval process",
    "Product2": "Products or services your company sells",
    "Profile": "User profiles controlling baseline permissions",
    "Quote": "Sales quotes/proposals for opportunities",
    "QuoteLineItem": "Individual line items within a quote",
    "RecordType": "Record types that segment object data by business process",
    "Report": "Salesforce reports (metadata only)",
    "SetupEntityAccess": "Access grants to specific setup entities",
    "Task": "To-do items, calls, and other activities",
    "TaskRelation": "Links between tasks and related contacts/leads",
    "UndecidedEventRelation": "Event invitees who haven't responded",
    "User": "Salesforce user accounts",
    "UserRole": "Roles in the role hierarchy for record visibility",
}


def _get_object_description(obj_name: str, obj_label: str, db_desc: str | None) -> str:
    """Return the best available description for a Salesforce object."""
    # 1. Database description (from SF API, usually empty).
    if db_desc:
        return db_desc
    # 2. Curated dictionary.
    if obj_name in SF_OBJECT_DESCRIPTIONS:
        return SF_OBJECT_DESCRIPTIONS[obj_name]
    # 3. Auto-generate for custom objects.
    if obj_name.endswith("__c"):
        clean = obj_name.replace("__c", "").replace("_", " ")
        return f"Custom object: {clean}"
    # 4. Use label if different from API name.
    if obj_label and obj_label != obj_name:
        return f"{obj_label}"
    return ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _get_conn():
    try:
        return db.get_connection()
    except FileNotFoundError as exc:
        abort(503, description=str(exc))


def _url_for_record(object_name: str, record_id: str) -> str:
    return url_for("sf.record_view", object_name=object_name, record_id=record_id)


def _field_type_map(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f["field_name"]: f for f in fields}


def _get_prefix_map(conn):
    """Return cached prefix_map (built once per request)."""
    from flask import g
    if "_prefix_map" not in g:
        g._prefix_map = db.build_prefix_map(conn)
    return g._prefix_map


def _get_export_meta(conn):
    """Return cached export metadata (built once per request)."""
    from flask import g
    if "_export_meta" not in g:
        g._export_meta = db.get_export_metadata(conn)
    return g._export_meta


@bp.after_app_request
def _inject_export_meta(response):
    """Make export_meta available in all templates."""
    return response


@bp.app_context_processor
def _context_processor():
    """Inject export_meta into every template context."""
    try:
        conn = db.get_connection()
        return {"export_meta": _get_export_meta(conn)}
    except Exception:
        return {"export_meta": None}


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #


def _history_parent_lookup(conn) -> dict[str, str]:
    """Return ``{history_table: parent_object}`` from ``_sf_relationships``.

    Used on the dashboard so each history row links to the object whose
    changes it tracks. When a history table has multiple inbound parents
    we prefer the one whose name prefixes the history table — that's the
    "natural" parent (``AccountHistory`` -> ``Account`` rather than
    ``Account`` -> ``...`` via some random reference).
    """
    try:
        rows = conn.execute(
            "SELECT from_object, to_object FROM _sf_relationships "
            "WHERE to_object IS NOT NULL AND to_object != ''"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for r in rows:
        h, p = r["from_object"], r["to_object"]
        if not field_history.is_history_table_name(h):
            continue
        if h not in out or h.startswith(p):
            out[h] = p
    return out


@bp.route("/")
def dashboard():
    conn = _get_conn()
    objects = db.list_objects(conn)
    total_objects = len(objects)
    total_records = sum(o["record_count"] or 0 for o in objects)
    exported_at = objects[0]["exported_at"] if objects else None
    custom_count = sum(1 for o in objects if o["custom"])

    # Separate key objects and categorize.
    key_set = set(KEY_OBJECTS)
    key_objects = [o for o in objects if o["name"] in key_set]
    # Sort key objects by KEY_OBJECTS order.
    key_order = {name: i for i, name in enumerate(KEY_OBJECTS)}
    key_objects.sort(key=lambda o: key_order.get(o["name"], 999))

    # Pull *History / *__History out into a dedicated category so they
    # don't drown out regular business objects in "All objects with data".
    history_set = {
        o["name"] for o in objects
        if field_history.is_history_table_name(o["name"])
    }
    parent_map = _history_parent_lookup(conn) if history_set else {}
    history_objects = [
        o for o in objects
        if o["name"] in history_set and (o["record_count"] or 0) > 0
    ]
    for o in history_objects:
        o["tracks_object"] = parent_map.get(o["name"])
    history_objects.sort(key=lambda o: -(o["record_count"] or 0))
    history_total_records = sum(o["record_count"] or 0 for o in history_objects)

    non_empty = [
        o for o in objects
        if (o["record_count"] or 0) > 0
        and o["name"] not in key_set
        and o["name"] not in history_set
    ]
    empty = [
        o for o in objects
        if (o["record_count"] or 0) == 0
        and o["name"] not in key_set
        and o["name"] not in history_set
    ]

    # Enrich objects with descriptions.
    for o in key_objects + non_empty + empty + history_objects:
        o["description"] = _get_object_description(
            o["name"], o.get("label", ""), o.get("description"),
        )

    return render_template(
        "dashboard.html",
        objects=objects,
        key_objects=key_objects,
        non_empty_objects=non_empty,
        empty_objects=empty,
        history_objects=history_objects,
        history_total_records=history_total_records,
        total_objects=total_objects,
        total_records=total_records,
        exported_at=exported_at,
        custom_count=custom_count,
        non_empty_count=len(non_empty) + len(key_objects) + len(history_objects),
        empty_count=len(empty),
    )


@bp.route("/api/objects")
def api_objects():
    conn = _get_conn()
    return jsonify(db.list_objects(conn))


# --------------------------------------------------------------------------- #
# Table view
# --------------------------------------------------------------------------- #


def _collect_filters(
    request_args, valid_cols: set[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (active_filters, open_filters).

    active_filters: columns with a non-empty value (applied to SQL).
    open_filters: ALL filter columns including empty ones (shown in UI).
    """
    active: dict[str, str] = {}
    open_filters: dict[str, str] = {}
    for key, val in request_args.items():
        if key.startswith("f_"):
            col = key[2:]
            if col in valid_cols:
                open_filters[col] = val or ""
                if val:
                    active[col] = val
    return active, open_filters


def _looks_like_sf_id(val: str) -> bool:
    """Match a 15- or 18-char Salesforce record Id (alphanumeric)."""
    if not isinstance(val, str):
        return False
    if len(val) not in (15, 18):
        return False
    return val.replace("_", "").isalnum()


def _target_object_for(field: dict[str, Any]) -> str | None:
    """Pick the first referenced object for a reference field, if any."""
    ref = field.get("reference_to") or ""
    ref = ref.strip()
    if not ref:
        return None
    return ref.split(",")[0].strip() or None


@bp.route("/table/<object_name>")
def table_view(object_name: str):
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404)

    fields = db.list_fields(conn, object_name)
    fields_map = _field_type_map(fields)
    all_cols = db.table_columns(conn, object_name)
    valid_cols = set(all_cols)
    prefix_map = _get_prefix_map(conn)

    # Smart column selection: show ~12 key columns unless user requests all.
    show_all = request.args.get("all_cols") == "1"
    if show_all:
        display_cols = all_cols
    else:
        display_cols = db.smart_columns(all_cols, fields)

    page = max(int(request.args.get("page", 1)), 1)
    page_size = min(max(int(request.args.get("page_size", 50)), 1), 500)
    sort = request.args.get("sort")
    sort_dir = request.args.get("dir", "asc")
    active_filters, open_filters = _collect_filters(request.args, valid_cols)

    # Soft-deleted records (IsDeleted=1, captured by `make export-shutdown`)
    # are hidden by default to match Salesforce's UI behaviour. The user can
    # opt in via `?show_deleted=1`. Tables without IsDeleted are unaffected.
    has_deleted_col = "IsDeleted" in valid_cols
    show_deleted = request.args.get("show_deleted") == "1"
    hide_deleted = has_deleted_col and not show_deleted
    deleted_count = db.count_deleted(conn, object_name) if has_deleted_col else 0

    # Resolve reference filters from names to lists of IDs.
    # If the user typed a string that doesn't look like an SF Id, treat it as a
    # name search on the referenced object and convert to an IN clause.
    resolved_filters: dict[str, Any] = dict(active_filters)
    filter_display: dict[str, str] = {}  # col → human label shown in chip
    for col, val in list(active_filters.items()):
        fm = fields_map.get(col)
        if not fm or fm.get("sf_type") != "reference":
            continue
        if _looks_like_sf_id(val):
            continue  # Plain Id filter — keep LIKE behaviour.
        target = _target_object_for(fm)
        if not target:
            continue
        ids = db.resolve_name_to_ids(conn, target, val)
        # Empty list forces zero results rather than falling through to an
        # unfiltered query.
        resolved_filters[col] = ids
        filter_display[col] = f"{val} → {len(ids)} match{'es' if len(ids) != 1 else ''}"

    rows, total = db.fetch_records(
        conn,
        object_name,
        page=page,
        page_size=page_size,
        sort=sort,
        sort_dir=sort_dir,
        filters=resolved_filters,
        hide_deleted=hide_deleted,
    )
    total_pages = max(1, (total + page_size - 1) // page_size)

    row_dicts = [dict(r) for r in rows]

    # Batch-resolve reference names for the current page.
    table_names = db.batch_resolve_table_names(
        conn, row_dicts, display_cols, fields, prefix_map,
    )

    # Build list of filterable columns (reference, picklist, string types).
    filterable_cols = []
    for col in all_cols:
        fm = fields_map.get(col)
        if fm and fm["sf_type"] in ("string", "picklist", "reference", "textarea",
                                     "email", "phone", "url", "id"):
            filterable_cols.append(col)
        elif col in ("Id", "Name"):
            if col not in filterable_cols:
                filterable_cols.append(col)

    # Pre-fetch distinct values for ALL open filter columns (including empty).
    # For reference columns we fetch names (resolved from the target object's
    # name column) so the autocomplete dropdown shows things like "Carvago
    # s.r.o." instead of raw IDs.
    filter_suggestions: dict[str, list[str]] = {}
    for col in open_filters:
        fm = fields_map.get(col)
        if not fm:
            continue
        if fm["sf_type"] == "reference":
            target = _target_object_for(fm)
            if target:
                refs = db.distinct_reference_names(conn, object_name, col, target, limit=200)
                # De-dup labels (multiple IDs can share the same name).
                seen = set()
                names: list[str] = []
                for r in refs:
                    label = r["label"]
                    if label and label not in seen:
                        seen.add(label)
                        names.append(label)
                filter_suggestions[col] = names
        elif fm["sf_type"] in ("picklist", "multipicklist", "string"):
            filter_suggestions[col] = db.distinct_values(conn, object_name, col, limit=30)

    obj_description = _get_object_description(
        obj["name"], obj.get("label", ""), obj.get("description"),
    )

    return render_template(
        "table.html",
        obj=obj,
        obj_description=obj_description,
        fields=fields,
        fields_map=fields_map,
        cols=display_cols,
        all_cols_count=len(all_cols),
        show_all=show_all,
        rows=row_dicts,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        sort=sort,
        sort_dir=sort_dir,
        filters=open_filters,
        filter_suggestions=filter_suggestions,
        filter_display=filter_display,
        filterable_cols=filterable_cols,
        render_cell=render_cell,
        url_for_record=_url_for_record,
        prefix_map=prefix_map,
        table_names=table_names,
        has_deleted_col=has_deleted_col,
        show_deleted=show_deleted,
        deleted_count=deleted_count,
    )


@bp.route("/api/table/<object_name>/distinct/<column>")
def api_distinct(object_name: str, column: str):
    """Return distinct values for a column (used for filter autocomplete).

    For reference-typed columns returns ``{values: [names…], references:
    [{id, label, count}]}`` so the UI can show resolved names; otherwise a
    plain ``{values: [...]}`` list of distinct raw values.
    """
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404)
    fields = db.list_fields(conn, object_name)
    fields_map = _field_type_map(fields)
    fm = fields_map.get(column)
    if fm and fm["sf_type"] == "reference":
        target = _target_object_for(fm)
        if target:
            refs = db.distinct_reference_names(conn, object_name, column, target, limit=200)
            seen: set[str] = set()
            names: list[str] = []
            for r in refs:
                label = r["label"]
                if label and label not in seen:
                    seen.add(label)
                    names.append(label)
            return jsonify({"column": column, "values": names, "references": refs})
    values = db.distinct_values(conn, object_name, column, limit=50)
    return jsonify({"column": column, "values": values})


@bp.route("/api/table/<object_name>")
def api_table(object_name: str):
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404)
    cols = db.table_columns(conn, object_name)
    valid_cols = set(cols)
    page = max(int(request.args.get("page", 1)), 1)
    page_size = min(max(int(request.args.get("page_size", 50)), 1), 500)
    sort = request.args.get("sort")
    sort_dir = request.args.get("dir", "asc")
    active_filters, _ = _collect_filters(request.args, valid_cols)
    rows, total = db.fetch_records(
        conn,
        object_name,
        page=page,
        page_size=page_size,
        sort=sort,
        sort_dir=sort_dir,
        filters=active_filters,
    )
    return jsonify(
        {
            "object": object_name,
            "total": total,
            "page": page,
            "page_size": page_size,
            "records": [dict(r) for r in rows],
        }
    )


@bp.route("/table/<object_name>/export.csv")
def export_table_csv(object_name: str):
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404)
    cols = db.table_columns(conn, object_name)
    valid_cols = set(cols)
    sort = request.args.get("sort")
    sort_dir = request.args.get("dir", "asc")
    active_filters, _ = _collect_filters(request.args, valid_cols)
    # Export up to a hard cap; anyone needing more should query SQLite directly.
    rows, _total = db.fetch_records(
        conn,
        object_name,
        page=1,
        page_size=50_000,
        sort=sort,
        sort_dir=sort_dir,
        filters=active_filters,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(cols)
    for row in rows:
        writer.writerow([row[c] for c in cols])
    csv_bytes = buffer.getvalue().encode("utf-8")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{object_name}.csv"'
        },
    )


# --------------------------------------------------------------------------- #
# Record detail
# --------------------------------------------------------------------------- #


def _load_record_context(object_name: str, record_id: str):
    """Shared data loader for both record views. Returns a dict or a Response
    (record_not_found page) to be returned by the caller as-is.
    """
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404, description=f"Object '{object_name}' not found in this export.")
    record = db.get_record(conn, object_name, record_id)
    if record is None:
        prefix_map = _get_prefix_map(conn)
        return render_template(
            "record_not_found.html",
            obj=obj,
            record_id=record_id,
            prefix_map=prefix_map,
        ), 404

    fields = db.list_fields(conn, object_name)
    fields_map = _field_type_map(fields)
    prefix_map = _get_prefix_map(conn)
    name_cache = db.batch_resolve_names(conn, record, fields, prefix_map)
    related = db.related_records(conn, object_name, record_id)
    outbound = db.outbound_relationships(conn, object_name)

    email_context = None
    if object_name == "EmailMessage":
        email_context = _build_email_context(conn, record, prefix_map)

    return {
        "conn": conn,
        "obj": obj,
        "record": record,
        "fields": fields,
        "fields_map": fields_map,
        "prefix_map": prefix_map,
        "name_cache": name_cache,
        "related": related,
        "outbound": outbound,
        "email_context": email_context,
    }


@bp.route("/table/<object_name>/<record_id>")
def record_view(object_name: str, record_id: str):
    """Keboola view — Lightning-inspired business layout (default)."""
    ctx = _load_record_context(object_name, record_id)
    if isinstance(ctx, tuple):  # record not found page
        return ctx

    obj = ctx["obj"]
    record = ctx["record"]
    fields = ctx["fields"]
    conn = ctx["conn"]
    prefix_map = ctx["prefix_map"]
    record_sections = layout.sections(fields, record)
    record_highlights = layout.highlights(fields, record)
    priority_related, more_related = layout.related_priority(obj["name"], ctx["related"])

    # Per-child field maps & name caches so related-list rows render
    # references as links, booleans as checkmarks, etc. — just like the main
    # record grid.
    child_fields_map: dict[str, dict[str, Any]] = {}
    child_name_cache: dict[str, dict[str, tuple[str, str]]] = {}
    for group in priority_related:
        child = group["object"]
        if child in child_fields_map:
            continue
        child_fields = db.list_fields(conn, child)
        child_fields_map[child] = _field_type_map(child_fields)
        # Collect reference IDs in preview rows and batch-resolve their Names.
        ref_cols = [f["field_name"] for f in child_fields if f["sf_type"] == "reference"]
        all_ids: dict[str, set[str]] = {}
        for row in group["preview"]:
            for col in ref_cols:
                val = row.get(col)
                if val and isinstance(val, str) and len(val) >= 15:
                    tgt = db.resolve_object_for_id(prefix_map, val)
                    if tgt:
                        all_ids.setdefault(tgt, set()).add(val)
        resolved: dict[str, tuple[str, str]] = {}
        for tgt, ids in all_ids.items():
            if not db.is_safe_ident(tgt):
                continue
            cols = db.table_columns(conn, tgt)
            name_col = next((c for c in ("Name", "Subject", "Title") if c in cols), None)
            if not name_col:
                continue
            placeholders = ",".join("?" for _ in ids)
            try:
                rows_res = conn.execute(
                    f'SELECT "Id", "{name_col}" FROM "{tgt}" WHERE "Id" IN ({placeholders})',
                    list(ids),
                ).fetchall()
                for r in rows_res:
                    if r[name_col]:
                        resolved[r["Id"]] = (tgt, r[name_col])
            except Exception:  # noqa: BLE001
                continue
        child_name_cache[child] = resolved

    # Unified activity timeline (Task + Event + EmailMessage).
    activity_timeline = activity.build_timeline(conn, obj["name"], record["Id"])

    # Field-level audit log (AccountHistory, *FieldHistory, *__History).
    # Empty list when the org doesn't track this object or the export
    # didn't include `--include-history`.
    field_changes = field_history.build_field_history(
        conn, obj["name"], record["Id"]
    )

    # Attached binary files (Attachments + ContentDocumentLink → ContentVersion).
    record_files = sf_files.files_for_record(conn, obj["name"], record["Id"])

    return render_template(
        "record.html",
        obj=obj,
        record=record,
        fields=fields,
        fields_map=ctx["fields_map"],
        highlights=record_highlights,
        sections=record_sections,
        priority_related=priority_related,
        more_related=more_related,
        outbound=ctx["outbound"],
        render_cell=render_cell,
        render_keboola=render_keboola,
        url_for_record=_url_for_record,
        salesforce_id=salesforce_id,
        prefix_map=prefix_map,
        name_cache=ctx["name_cache"],
        email_context=ctx["email_context"],
        record_title=layout.record_title(record, obj["name"]),
        related_columns=layout.related_columns,
        child_fields_map=child_fields_map,
        child_name_cache=child_name_cache,
        activity_timeline=activity_timeline,
        field_changes=field_changes,
        record_files=record_files,
        view_mode="keboola",
    )


@bp.route("/raw/table/<object_name>/<record_id>")
def record_view_raw(object_name: str, record_id: str):
    """Raw view — generic dump of every field and every related list.

    Kept available as a power-user/forensics view during Salesforce
    off-boarding. Linked from every Keboola record page.
    """
    ctx = _load_record_context(object_name, record_id)
    if isinstance(ctx, tuple):
        return ctx

    fields = ctx["fields"]
    record = ctx["record"]

    priority_fields: list[dict[str, Any]] = []
    populated_fields: list[dict[str, Any]] = []
    empty_fields: list[dict[str, Any]] = []

    for pf in PRIORITY_FIELDS:
        for f in fields:
            if f["field_name"] == pf:
                priority_fields.append(f)
                break

    seen = {f["field_name"] for f in priority_fields}
    for f in fields:
        if f["field_name"] in seen:
            continue
        val = record.get(f["field_name"])
        if val is not None and val != "":
            populated_fields.append(f)
        else:
            empty_fields.append(f)

    return render_template(
        "record_raw.html",
        obj=ctx["obj"],
        record=record,
        fields=fields,
        fields_map=ctx["fields_map"],
        priority_fields=priority_fields,
        populated_fields=populated_fields,
        empty_fields=empty_fields,
        related=ctx["related"],
        outbound=ctx["outbound"],
        render_cell=render_cell,
        url_for_record=_url_for_record,
        salesforce_id=salesforce_id,
        prefix_map=ctx["prefix_map"],
        name_cache=ctx["name_cache"],
        email_context=ctx["email_context"],
        view_mode="raw",
    )


def _build_email_context(
    conn,
    record: dict[str, Any],
    prefix_map: dict[str, str],
) -> dict[str, Any]:
    """Build enhanced context for EmailMessage records."""
    ctx: dict[str, Any] = {}

    # Resolve RelatedTo (Account, Opportunity, etc.).
    related_to_id = record.get("RelatedToId")
    if related_to_id:
        obj_name = db.resolve_object_for_id(prefix_map, related_to_id)
        if obj_name:
            name = db.resolve_record_name(conn, obj_name, related_to_id)
            ctx["related_to"] = {
                "id": related_to_id,
                "object": obj_name,
                "name": name or related_to_id,
            }

    # Resolve Activity (Task).
    activity_id = record.get("ActivityId")
    if activity_id:
        obj_name = db.resolve_object_for_id(prefix_map, activity_id)
        if obj_name:
            name = db.resolve_record_name(conn, obj_name, activity_id)
            ctx["activity"] = {
                "id": activity_id,
                "object": obj_name or "Task",
                "name": name or activity_id,
            }

    # Get EmailMessageRelation participants.
    try:
        cols = db.table_columns(conn, "EmailMessageRelation")
        if "EmailMessageId" in cols:
            rows = conn.execute(
                'SELECT * FROM "EmailMessageRelation" WHERE "EmailMessageId" = ? '
                'ORDER BY "RelationType"',
                (record["Id"],),
            ).fetchall()
            participants = []
            for r in rows:
                p = dict(r)
                # Resolve RelationId to a Contact/Lead/User name.
                rel_id = p.get("RelationId")
                if rel_id:
                    rel_obj = db.resolve_object_for_id(prefix_map, rel_id)
                    if rel_obj:
                        p["_relation_object"] = rel_obj
                        p["_relation_name"] = db.resolve_record_name(conn, rel_obj, rel_id)
                participants.append(p)
            ctx["participants"] = participants
    except Exception:
        pass

    # Reply chain.
    reply_to_id = record.get("ReplyToEmailMessageId")
    if reply_to_id:
        reply_rec = db.get_record(conn, "EmailMessage", reply_to_id)
        if reply_rec:
            ctx["reply_to"] = {
                "id": reply_to_id,
                "subject": reply_rec.get("Subject") or reply_rec.get("Name") or reply_to_id,
            }

    return ctx


@bp.route("/api/table/<object_name>/<record_id>")
def api_record(object_name: str, record_id: str):
    conn = _get_conn()
    record = db.get_record(conn, object_name, record_id)
    if record is None:
        abort(404)
    return jsonify(record)


# --------------------------------------------------------------------------- #
# Schema browser
# --------------------------------------------------------------------------- #


@bp.route("/schema/<object_name>")
def schema_view(object_name: str):
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404)
    fields = db.list_fields(conn, object_name)
    inbound = db.inbound_relationships(conn, object_name)
    outbound = db.outbound_relationships(conn, object_name)
    return render_template(
        "schema.html",
        obj=obj,
        fields=fields,
        inbound=inbound,
        outbound=outbound,
    )


@bp.route("/api/schema/<object_name>")
def api_schema(object_name: str):
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404)
    return jsonify(
        {
            "object": obj,
            "fields": db.list_fields(conn, object_name),
            "inbound": db.inbound_relationships(conn, object_name),
            "outbound": db.outbound_relationships(conn, object_name),
        }
    )


# --------------------------------------------------------------------------- #
# Global search
# --------------------------------------------------------------------------- #


@bp.route("/search")
def search_view():
    query = request.args.get("q", "").strip()
    results: list[dict[str, Any]] = []
    if query:
        conn = _get_conn()
        results = db.global_search(conn, query)
    return render_template("search.html", query=query, results=results)


@bp.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"query": "", "results": []})
    conn = _get_conn()
    return jsonify({"query": query, "results": db.global_search(conn, query)})


# --------------------------------------------------------------------------- #
# SQL query editor
# --------------------------------------------------------------------------- #


@bp.route("/query", methods=["GET"])
def query_view():
    return render_template("query.html")


@bp.route("/api/query", methods=["POST"])
def api_query():
    payload = request.get_json(silent=True) or {}
    sql = (payload.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "empty query"}), 400
    conn = _get_conn()
    try:
        result = db.execute_readonly_query(conn, sql)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400
    return jsonify(result)


# --------------------------------------------------------------------------- #
# Migration — field stats, DDL generation, selective CSV export
# --------------------------------------------------------------------------- #


def _parse_fields_param(raw: str | None) -> list[str]:
    """Parse ``?fields=Id,Name,Type`` / form ``fields=...`` into a list.

    Accepts comma-separated values or repeated form fields. Empty entries
    are discarded.
    """
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


@bp.route("/migrate")
def migrate_index():
    """Listing page — every exported object linked to its migration report."""
    conn = _get_conn()
    objects = db.list_objects(conn)

    # Pre-compute field counts in one query to avoid N lookups.
    field_counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT object_name, COUNT(*) AS c FROM _sf_fields GROUP BY object_name"
    ).fetchall():
        field_counts[row["object_name"]] = int(row["c"] or 0)

    enriched = []
    for o in objects:
        enriched.append(
            {
                "name": o["name"],
                "label": o.get("label") or o["name"],
                "custom": bool(o.get("custom")),
                "record_count": int(o.get("record_count") or 0),
                "field_count": field_counts.get(o["name"], 0),
            }
        )

    return render_template("migrate_index.html", objects=enriched)


@bp.route("/migrate/<object_name>")
def migrate_object(object_name: str):
    """Migration report for one object: stats, DDL preview, export form."""
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404, description=f"Object '{object_name}' not found in this export.")

    stats = migration.field_stats(conn, object_name)

    # Summary metrics.
    total_fields = len(stats)
    populated = sum(1 for s in stats if (s["fill_rate"] or 0) > 0)
    custom_fields = sum(1 for s in stats if s["custom"])
    dead_fields = [s for s in stats if (s["fill_rate"] or 0) == 0]
    avg_fill = (
        sum((s["fill_rate"] or 0) for s in stats) / total_fields
        if total_fields
        else 0.0
    )

    return render_template(
        "migrate.html",
        obj=obj,
        stats=stats,
        total_fields=total_fields,
        populated_fields=populated,
        custom_fields=custom_fields,
        dead_fields=dead_fields,
        avg_fill=avg_fill,
    )


@bp.route("/api/migrate/<object_name>/stats")
def api_migrate_stats(object_name: str):
    """JSON stats feed — same data as the HTML page."""
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404)
    stats = migration.field_stats(conn, object_name)
    return jsonify(stats)


@bp.route("/migrate/<object_name>/ddl")
def migrate_ddl(object_name: str):
    """Return a ``CREATE TABLE`` for the selected fields as text/plain."""
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404)

    fields_arg = request.args.get("fields", "")
    names = _parse_fields_param(fields_arg)
    # Pull full metadata for the selected fields only.
    all_fields = db.list_fields(conn, object_name)
    selected_set = set(names) if names else {f["field_name"] for f in all_fields}
    ordered = [f for f in all_fields if f["field_name"] in selected_set]
    # If the caller provided an explicit order, honour it.
    if names:
        by_name = {f["field_name"]: f for f in ordered}
        ordered = [by_name[n] for n in names if n in by_name]

    ddl = migration.generate_ddl(
        object_name,
        ordered,
        dialect="postgres",
        exported_at=obj.get("exported_at"),
    )
    return Response(ddl, mimetype="text/plain; charset=utf-8")


@bp.route("/migrate/<object_name>/export.csv", methods=["POST", "GET"])
def migrate_export_csv(object_name: str):
    """Stream a CSV containing only the requested fields."""
    conn = _get_conn()
    obj = db.get_object(conn, object_name)
    if not obj:
        abort(404)

    # Form POST may have either repeated `fields=Id&fields=Name` or a single
    # comma-separated `fields=Id,Name`.
    raw_values = request.values.getlist("fields")
    names: list[str] = []
    for item in raw_values:
        names.extend(_parse_fields_param(item))

    if not names:
        abort(400, description="No fields selected for export.")

    # Build the stream iterator eagerly — `export_csv_stream` validates the
    # column list synchronously (while `conn` is still open) and returns an
    # iterator that will stream rows from a fresh connection.
    stream = migration.export_csv_stream(conn, object_name, names)

    return Response(
        stream,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{object_name}_migration.csv"'
            ),
        },
    )


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


@bp.app_errorhandler(404)
def _handle_404(err):
    return render_template("error.html", code=404, message=str(err)), 404


@bp.app_errorhandler(503)
def _handle_503(err):
    return render_template("error.html", code=503, message=str(err)), 503


# --------------------------------------------------------------------------- #
# Downloaded binary files (Attachments / ContentVersion)
# --------------------------------------------------------------------------- #


@bp.route("/files/<file_id>")
def serve_file(file_id: str):
    """Serve a downloaded Attachment / ContentVersion / Document by Salesforce Id.

    The file must have been previously written by
    ``scripts/download_files.py`` and recorded in ``manifest.json``.
    """
    from flask import send_file  # local import to keep top imports lean

    path = sf_files.resolve_file_path(file_id)
    if not path:
        abort(404, description=f"File {file_id} not downloaded.")
    entry = sf_files.lookup_manifest_entry(file_id) or {}
    download_name = entry.get("name") or entry.get("title") or path.name
    return send_file(
        path,
        download_name=download_name,
        as_attachment=False,  # inline preview when browser supports (PDFs, images)
    )


# Teardown helper for app factory.
def close_db(exc=None):
    db.close_db(exc)
