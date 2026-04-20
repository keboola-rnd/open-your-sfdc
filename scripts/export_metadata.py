#!/usr/bin/env python3
"""
Export Salesforce metadata artefacts that are NOT queryable via standard REST.

Most metadata is queryable via the Tooling API only. This script covers:

  Flow / FlowDefinition           — Lightning flows + active-version pointers
  ValidationRule                  — field-level validation formulas
  WorkflowRule, WorkflowFieldUpdate, WorkflowTask, WorkflowOutboundMessage
  LightningComponentBundle        — LWC bundles
  LightningComponentResource      — LWC source files (HTML / JS / CSS)
  CustomField                     — formulas, defaults, picklist sources
  InstalledSubscriberPackage      — AppExchange packages with namespace + version
  CronTrigger / CronJobDetail     — scheduled Apex and other cron jobs

Outputs:
  data/sf_metadata/<Type>/<Name>.json   — per-record full Metadata blob
  data/sf_metadata/_index.json          — summary: counts + timestamps + errors

Usage:
    python scripts/export_metadata.py                 # Export everything
    python scripts/export_metadata.py --dry-run       # Counts only, no fetch
    python scripts/export_metadata.py --resume        # Skip files already saved
    python scripts/export_metadata.py --types flow    # Only Flow + FlowDefinition
    python scripts/export_metadata.py --types flow,validation,workflow

Requires .env with Salesforce credentials (see sf_connect.py).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"
METADATA_DIR = DATA_DIR / "sf_metadata"

from scripts.sf_connect import get_connection  # noqa: E402

try:
    from simple_salesforce import Salesforce
    from simple_salesforce.exceptions import SalesforceError
except ImportError:
    print("Error: simple-salesforce is not installed.")
    print("Run: pip install simple-salesforce")
    sys.exit(1)


# The Tooling API version the underlying simple_salesforce session already uses.
# We reuse sf.base_url which embeds it (e.g. /services/data/v59.0/).
# For tooling endpoints we prefix with "tooling/".
TOOLING_PREFIX = "tooling/"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_filename(name: str) -> str:
    """Strip characters that are unsafe in file names and cap length."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip()
    return (name or "unnamed")[:200]


# -----------------------------------------------------------------------------
# Tooling API helpers
# -----------------------------------------------------------------------------

_NEXT_URL_RE = re.compile(r"/services/data/v\d+\.\d+/(?P<rel>.*)")


def tooling_query(sf: Salesforce, soql: str) -> list[dict]:
    """Run a SOQL query against the Tooling API and follow pagination.

    ``sf.restful`` takes a path relative to ``/services/data/v<X>.<Y>/``.
    ``nextRecordsUrl`` in the response is absolute (``/services/data/…``),
    so we strip the version prefix with a regex that works regardless of
    the simple_salesforce version.
    """
    path = f"{TOOLING_PREFIX}query/?q={soql}"
    result = sf.restful(path, method="GET")
    records = list(result.get("records", []))
    while not result.get("done", True) and result.get("nextRecordsUrl"):
        next_url = result["nextRecordsUrl"]
        m = _NEXT_URL_RE.match(next_url)
        rel = m.group("rel") if m else next_url.lstrip("/")
        result = sf.restful(rel, method="GET")
        records.extend(result.get("records", []))
    return records


def tooling_sobject(sf: Salesforce, object_type: str, record_id: str) -> dict:
    """Retrieve a single Tooling sObject record by Id (includes the Metadata field)."""
    path = f"{TOOLING_PREFIX}sobjects/{object_type}/{record_id}"
    return sf.restful(path, method="GET") or {}


# -----------------------------------------------------------------------------
# Per-type exporters. Each returns (records_written, errors).
# -----------------------------------------------------------------------------

def _write_record(
    type_dir: Path,
    filename: str,
    record: dict,
    *,
    resume: bool,
) -> bool:
    """Write one metadata record to JSON. Returns True if a file was (re)written."""
    type_dir.mkdir(parents=True, exist_ok=True)
    out = type_dir / f"{safe_filename(filename)}.json"
    if resume and out.exists() and out.stat().st_size > 0:
        return False
    with open(out, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False, default=str)
    return True


def export_flow(
    sf: Salesforce,
    out_dir: Path,
    *,
    dry_run: bool,
    resume: bool,
) -> dict:
    """Export all Flow versions + FlowDefinition pointers.

    Flow.Metadata can only be queried one record at a time (SF constraint).
    We first enumerate, then fetch the single-record resource for each Id.
    """
    print("\n=== Flow + FlowDefinition ===")
    stats = {"type": "Flow", "total": 0, "written": 0, "skipped": 0, "errors": 0}
    errors: list[str] = []

    # 1. Enumerate all Flow versions (no Metadata to avoid the single-record restriction).
    soql = (
        "SELECT Id, DeveloperName, MasterLabel, Status, VersionNumber, "
        "ProcessType, ApiVersion, LastModifiedDate, ManageableState "
        "FROM Flow ORDER BY DeveloperName, VersionNumber"
    )
    try:
        flows = tooling_query(sf, soql)
    except SalesforceError as exc:
        print(f"  ! Could not enumerate Flow: {exc}")
        return {**stats, "errors": 1, "_error_detail": str(exc)}

    print(f"  Found {len(flows)} Flow version(s)")
    stats["total"] = len(flows)

    if not dry_run:
        type_dir = out_dir / "Flow"
        for i, flow in enumerate(flows, start=1):
            dev_name = flow.get("DeveloperName", flow["Id"])
            version = flow.get("VersionNumber", 0)
            filename = f"{dev_name}__v{version}"
            try:
                full = tooling_sobject(sf, "Flow", flow["Id"])
                if _write_record(type_dir, filename, full, resume=resume):
                    stats["written"] += 1
                else:
                    stats["skipped"] += 1
                if i % 25 == 0:
                    print(f"    ... {i}/{len(flows)} flow versions")
            except SalesforceError as exc:
                stats["errors"] += 1
                errors.append(f"Flow {dev_name} v{version}: {exc}")
                print(f"  ! Flow {dev_name} v{version}: {exc}")

    # 2. FlowDefinition: pointer to ActiveVersion + Latest, for easy lookup.
    fd_soql = (
        "SELECT Id, DeveloperName, MasterLabel, ActiveVersionId, "
        "LatestVersionId, Description, ManageableState "
        "FROM FlowDefinition ORDER BY DeveloperName"
    )
    try:
        defs = tooling_query(sf, fd_soql)
    except SalesforceError as exc:
        print(f"  ! Could not query FlowDefinition: {exc}")
        defs = []

    print(f"  Found {len(defs)} FlowDefinition(s)")
    if not dry_run and defs:
        def_dir = out_dir / "FlowDefinition"
        for fd in defs:
            dev_name = fd.get("DeveloperName", fd["Id"])
            if _write_record(def_dir, dev_name, fd, resume=resume):
                stats["written"] += 1
            else:
                stats["skipped"] += 1

    if errors:
        stats["_errors"] = errors[:20]
    return stats


def _export_with_metadata_per_id(
    sf: Salesforce,
    *,
    object_type: str,
    list_soql: str,
    name_field: str,
    out_dir: Path,
    dry_run: bool,
    resume: bool,
) -> dict:
    """Generic pattern: enumerate via SOQL, then fetch full record (incl. Metadata) per Id.

    Used for ValidationRule, WorkflowRule, WorkflowFieldUpdate, WorkflowTask,
    WorkflowOutboundMessage, CustomField — all Tooling sObjects whose
    ``Metadata`` field cannot appear in multi-row SOQL.
    """
    print(f"\n=== {object_type} ===")
    stats = {"type": object_type, "total": 0, "written": 0, "skipped": 0, "errors": 0}
    errors: list[str] = []

    try:
        records = tooling_query(sf, list_soql)
    except SalesforceError as exc:
        msg = str(exc)
        print(f"  ! Could not enumerate {object_type}: {exc}")
        return {**stats, "errors": 1, "_error_detail": msg}

    print(f"  Found {len(records)} record(s)")
    stats["total"] = len(records)
    if dry_run or not records:
        return stats

    type_dir = out_dir / object_type
    for i, rec in enumerate(records, start=1):
        raw_name = rec.get(name_field) or rec.get("FullName") or rec["Id"]
        # Validation rules are named like "Account.MyRule"; keep that prefix
        # to disambiguate rules with the same name across different objects.
        entity = rec.get("EntityDefinition") or {}
        qualifier = entity.get("QualifiedApiName") if isinstance(entity, dict) else None
        filename = f"{qualifier}.{raw_name}" if qualifier else raw_name
        try:
            full = tooling_sobject(sf, object_type, rec["Id"])
            if _write_record(type_dir, filename, full, resume=resume):
                stats["written"] += 1
            else:
                stats["skipped"] += 1
            if i % 50 == 0:
                print(f"    ... {i}/{len(records)}")
        except SalesforceError as exc:
            stats["errors"] += 1
            errors.append(f"{object_type} {raw_name}: {exc}")
            print(f"  ! {object_type} {raw_name}: {exc}")

    if errors:
        stats["_errors"] = errors[:20]
    return stats


def export_validation_rule(sf, out_dir, *, dry_run, resume):
    return _export_with_metadata_per_id(
        sf,
        object_type="ValidationRule",
        list_soql=(
            "SELECT Id, ValidationName, Active, Description, ErrorMessage, "
            "ErrorDisplayField, EntityDefinitionId, EntityDefinition.QualifiedApiName, "
            "CreatedById, CreatedDate, LastModifiedById, LastModifiedDate "
            "FROM ValidationRule ORDER BY EntityDefinition.QualifiedApiName, ValidationName"
        ),
        name_field="ValidationName",
        out_dir=out_dir,
        dry_run=dry_run,
        resume=resume,
    )


def export_workflow_rule(sf, out_dir, *, dry_run, resume):
    return _export_with_metadata_per_id(
        sf,
        object_type="WorkflowRule",
        list_soql=(
            "SELECT Id, FullName, Name, TableEnumOrId, CreatedDate, LastModifiedDate "
            "FROM WorkflowRule ORDER BY TableEnumOrId, Name"
        ),
        name_field="Name",
        out_dir=out_dir,
        dry_run=dry_run,
        resume=resume,
    )


def export_workflow_field_update(sf, out_dir, *, dry_run, resume):
    return _export_with_metadata_per_id(
        sf,
        object_type="WorkflowFieldUpdate",
        list_soql=(
            "SELECT Id, FullName, Name, TableEnumOrId "
            "FROM WorkflowFieldUpdate ORDER BY TableEnumOrId, Name"
        ),
        name_field="Name",
        out_dir=out_dir,
        dry_run=dry_run,
        resume=resume,
    )


def export_workflow_task(sf, out_dir, *, dry_run, resume):
    return _export_with_metadata_per_id(
        sf,
        object_type="WorkflowTask",
        list_soql=(
            "SELECT Id, FullName, Name, TableEnumOrId "
            "FROM WorkflowTask ORDER BY TableEnumOrId, Name"
        ),
        name_field="Name",
        out_dir=out_dir,
        dry_run=dry_run,
        resume=resume,
    )


def export_workflow_outbound_message(sf, out_dir, *, dry_run, resume):
    return _export_with_metadata_per_id(
        sf,
        object_type="WorkflowOutboundMessage",
        list_soql=(
            "SELECT Id, FullName, Name, TableEnumOrId "
            "FROM WorkflowOutboundMessage ORDER BY TableEnumOrId, Name"
        ),
        name_field="Name",
        out_dir=out_dir,
        dry_run=dry_run,
        resume=resume,
    )


def export_custom_field(sf, out_dir, *, dry_run, resume):
    """Export CustomField metadata including formulas, defaults, picklist sources.

    CustomField.Metadata has the single-record restriction too. This is the
    heaviest call in this whole script because there are usually many custom
    fields. ``--resume`` makes subsequent runs nearly free.
    """
    return _export_with_metadata_per_id(
        sf,
        object_type="CustomField",
        list_soql=(
            "SELECT Id, DeveloperName, TableEnumOrId, FullName, "
            "ManageableState, NamespacePrefix "
            "FROM CustomField ORDER BY TableEnumOrId, DeveloperName"
        ),
        name_field="DeveloperName",
        out_dir=out_dir,
        dry_run=dry_run,
        resume=resume,
    )


def export_lwc(
    sf: Salesforce,
    out_dir: Path,
    *,
    dry_run: bool,
    resume: bool,
) -> dict:
    """Export Lightning Web Components (bundles + every source file).

    LightningComponentResource.Source is available in multi-record SOQL, so
    this is much faster than the per-Id pattern above.
    """
    print("\n=== LightningComponentBundle + Resource (LWC) ===")
    stats = {"type": "LWC", "total": 0, "written": 0, "skipped": 0, "errors": 0}

    try:
        bundles = tooling_query(sf, (
            "SELECT Id, DeveloperName, MasterLabel, ApiVersion, Description, "
            "NamespacePrefix, ManageableState, CreatedDate, LastModifiedDate "
            "FROM LightningComponentBundle"
        ))
    except SalesforceError as exc:
        print(f"  ! Could not query LightningComponentBundle: {exc}")
        return {**stats, "errors": 1, "_error_detail": str(exc)}

    try:
        resources = tooling_query(sf, (
            "SELECT Id, LightningComponentBundleId, FilePath, Format, Source, "
            "LastModifiedDate "
            "FROM LightningComponentResource"
        ))
    except SalesforceError as exc:
        print(f"  ! Could not query LightningComponentResource: {exc}")
        return {**stats, "errors": 1, "_error_detail": str(exc)}

    # Resources in multi-source queries sometimes arrive with Source truncated to
    # the 131072-char soft limit. That's Salesforce's problem, not ours — the
    # file remains marked with its true Source field.
    print(f"  Found {len(bundles)} bundles, {len(resources)} source files")
    stats["total"] = len(resources)

    if dry_run:
        return stats

    bundle_by_id: dict[str, dict] = {b["Id"]: b for b in bundles}
    type_dir = out_dir / "LightningComponentBundle"
    type_dir.mkdir(parents=True, exist_ok=True)

    # Group resources by bundle and write a single JSON per bundle + the raw
    # source files next to it (so greppable without JSON parsing).
    by_bundle: dict[str, list[dict]] = {}
    for res in resources:
        bid = res.get("LightningComponentBundleId")
        if bid:
            by_bundle.setdefault(bid, []).append(res)

    for bid, bundle in bundle_by_id.items():
        dev_name = bundle.get("DeveloperName", bid)
        bundle_dir = type_dir / safe_filename(dev_name)
        manifest_path = bundle_dir / "_bundle.json"

        if resume and manifest_path.exists() and manifest_path.stat().st_size > 0:
            stats["skipped"] += 1
            continue

        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_files = by_bundle.get(bid, [])
        manifest = {
            "bundle": bundle,
            "resources": [{k: v for k, v in r.items() if k != "Source"}
                          for r in bundle_files],
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False, default=str)
        # Also drop each source file into the bundle folder with its original
        # FilePath (e.g. ``myComp/myComp.js``) so greps like ``rg pattern lwc/``
        # work directly.
        for res in bundle_files:
            rel = res.get("FilePath") or f"{res['Id']}.txt"
            rel = rel.lstrip("/")
            # FilePath often starts with ``lwc/<name>/...`` — strip the lwc/<name>/ part.
            rel_parts = rel.split("/")
            if rel_parts[:2] == ["lwc", dev_name]:
                rel = "/".join(rel_parts[2:]) or rel_parts[-1]
            target = bundle_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            source = res.get("Source") or ""
            target.write_text(source, encoding="utf-8")
        stats["written"] += 1

    return stats


def export_installed_package(
    sf: Salesforce,
    out_dir: Path,
    *,
    dry_run: bool,
    resume: bool,
) -> dict:
    """Export InstalledSubscriberPackage (AppExchange packages installed in the org)."""
    print("\n=== InstalledSubscriberPackage (AppExchange) ===")
    stats = {"type": "InstalledSubscriberPackage", "total": 0, "written": 0, "skipped": 0, "errors": 0}

    soql = (
        "SELECT Id, SubscriberPackageId, SubscriberPackage.Name, "
        "SubscriberPackage.NamespacePrefix, "
        "SubscriberPackageVersion.Id, SubscriberPackageVersion.Name, "
        "SubscriberPackageVersion.MajorVersion, "
        "SubscriberPackageVersion.MinorVersion, "
        "SubscriberPackageVersion.PatchVersion, "
        "SubscriberPackageVersion.IsManaged "
        "FROM InstalledSubscriberPackage "
        "ORDER BY SubscriberPackage.Name"
    )
    try:
        records = tooling_query(sf, soql)
    except SalesforceError as exc:
        print(f"  ! Could not query InstalledSubscriberPackage: {exc}")
        return {**stats, "errors": 1, "_error_detail": str(exc)}

    print(f"  Found {len(records)} installed package(s)")
    stats["total"] = len(records)
    if dry_run:
        return stats

    # Single file for all packages — it's small and convenient.
    out = out_dir / "InstalledSubscriberPackage.json"
    if resume and out.exists() and out.stat().st_size > 0:
        stats["skipped"] = len(records)
        return stats
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False, default=str)
    stats["written"] = len(records)
    return stats


def export_cron_trigger(
    sf: Salesforce,
    out_dir: Path,
    *,
    dry_run: bool,
    resume: bool,
) -> dict:
    """Export CronTrigger (scheduled Apex and other cron jobs).

    CronTrigger is queryable via the standard REST API, not Tooling, but we
    keep it here so the whole "org automation" picture lives in one place.
    """
    print("\n=== CronTrigger (scheduled jobs) ===")
    stats = {"type": "CronTrigger", "total": 0, "written": 0, "skipped": 0, "errors": 0}

    soql = (
        "SELECT Id, CronExpression, TimesTriggered, StartTime, EndTime, "
        "NextFireTime, PreviousFireTime, State, OwnerId, "
        "CronJobDetail.Name, CronJobDetail.JobType, TimeZoneSidKey "
        "FROM CronTrigger"
    )
    try:
        result = sf.query_all(soql)
        records = result.get("records", [])
    except SalesforceError as exc:
        print(f"  ! Could not query CronTrigger: {exc}")
        return {**stats, "errors": 1, "_error_detail": str(exc)}

    print(f"  Found {len(records)} cron trigger(s)")
    stats["total"] = len(records)
    if dry_run:
        return stats

    out = out_dir / "CronTrigger.json"
    if resume and out.exists() and out.stat().st_size > 0:
        stats["skipped"] = len(records)
        return stats
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False, default=str)
    stats["written"] = len(records)
    return stats


# -----------------------------------------------------------------------------
# Registry of exporters — used by --types CLI filter and the main loop.
# -----------------------------------------------------------------------------

EXPORTERS: dict[str, Callable[..., dict]] = {
    "flow": export_flow,
    "validation": export_validation_rule,
    "workflow-rule": export_workflow_rule,
    "workflow-field-update": export_workflow_field_update,
    "workflow-task": export_workflow_task,
    "workflow-outbound": export_workflow_outbound_message,
    "custom-field": export_custom_field,
    "lwc": export_lwc,
    "installed-package": export_installed_package,
    "cron": export_cron_trigger,
}

# "workflow" is shorthand for all four workflow sub-types.
EXPANSIONS: dict[str, list[str]] = {
    "workflow": [
        "workflow-rule",
        "workflow-field-update",
        "workflow-task",
        "workflow-outbound",
    ],
}


def parse_types(raw: str | None) -> list[str]:
    """Turn a comma-separated ``--types`` arg into a concrete exporter list."""
    if not raw:
        return list(EXPORTERS.keys())
    requested = [t.strip().lower() for t in raw.split(",") if t.strip()]
    result: list[str] = []
    for t in requested:
        if t in EXPANSIONS:
            result.extend(EXPANSIONS[t])
        elif t in EXPORTERS:
            result.append(t)
        else:
            valid = sorted(set(EXPORTERS) | set(EXPANSIONS))
            raise SystemExit(f"Unknown --types value: {t!r}. Valid: {', '.join(valid)}")
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    deduped: list[str] = []
    for t in result:
        if t not in seen:
            deduped.append(t)
            seen.add(t)
    return deduped


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Enumerate + count records but do not fetch Metadata bodies.")
    parser.add_argument("--resume", action="store_true",
                        help="Skip records whose target JSON already exists.")
    parser.add_argument("--types", default=None,
                        help="Comma-separated types: " + ", ".join(sorted(set(EXPORTERS) | set(EXPANSIONS))))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = parse_types(args.types)

    print("=" * 60)
    print("Salesforce Metadata Export (Tooling API)")
    print("=" * 60)
    print(f"Target directory: {METADATA_DIR}")
    print(f"Selected types: {', '.join(selected)}")
    if args.dry_run:
        print("Mode: DRY-RUN (no files will be written)")

    print("\nConnecting to Salesforce...")
    sf = get_connection()
    print(f"Connected to: {sf.sf_instance}")

    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    started = utc_now_iso()
    all_stats: list[dict] = []
    for key in selected:
        exporter = EXPORTERS[key]
        t0 = time.time()
        stats = exporter(sf, METADATA_DIR, dry_run=args.dry_run, resume=args.resume)
        stats["duration_sec"] = round(time.time() - t0, 2)
        all_stats.append(stats)
    finished = utc_now_iso()

    # Write the index unless this was a dry-run.
    if not args.dry_run:
        index = {
            "started_at": started,
            "finished_at": finished,
            "sf_instance": sf.sf_instance,
            "types": all_stats,
        }
        idx_path = METADATA_DIR / "_index.json"
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nIndex written: {idx_path}")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total_written = 0
    total_errors = 0
    for s in all_stats:
        print(f"  {s['type']:<30} total={s.get('total', 0):>6}  "
              f"written={s.get('written', 0):>6}  "
              f"skipped={s.get('skipped', 0):>6}  "
              f"errors={s.get('errors', 0):>3}  "
              f"({s.get('duration_sec', 0)}s)")
        total_written += s.get("written", 0)
        total_errors += s.get("errors", 0)
    print(f"\n  Total files written: {total_written}")
    print(f"  Total errors: {total_errors}")
    print(f"  Output: {METADATA_DIR}")
    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
