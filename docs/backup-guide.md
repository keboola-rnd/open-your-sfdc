# Backup Operations Guide

How to use `open-your-sfdc` to produce a complete, repeatable backup of a
Salesforce org — what each command does, whether it supports incremental runs,
how long it takes, and how to schedule the work around a deprecation deadline.

This is the operational companion to `README.md` (marketing and quickstart)
and `docs/architecture.md` (technical design).

---

## What "archive" means here

The full archive is everything `make archive` produces:

```
data/
├── sf_full_export/    one CSV per SObject + metadata.json
├── salesforce_full.db  typed SQLite with indexes + FTS5
├── sf_files/           binaries (attachments, ContentVersion, SDocs PDFs)
├── sf_metadata/        Tooling API artefacts (Flow, ValidationRule, …)
├── sf_sfdx_metadata/   XML metadata via SFDX (Layouts, Profiles, …)
└── sf_event_logs/      EventLogFile binaries (needs licence)
```

After a successful run your `data/` folder is a self-contained mirror of the
org — records, binaries, declarative logic, code. The Flask browser and any
agents you build read from `data/`; none of them call Salesforce again.

---

## Command reference

| Command | What it does | Idempotent? | Incremental? | Typical duration |
|---|---|---|---|---|
| `make archive` | Umbrella: export + import + files + files-gaps + metadata + sfdx-retrieve + event-logs + audit | Yes | Partially (each subcommand has its own policy) | 1.5–3 h first run, 30–60 min on repeat |
| `make archive-audit` | Reports what ran, what's missing | Read-only | — | seconds |
| `make export` | Dump every queryable SObject to CSV | Yes | **No — full refresh** | ~40 min |
| `make export-history` | `export` + `__History` tables | Yes | No | can be hours (millions of rows) |
| `make import` | Drop + rebuild `salesforce_full.db` from CSVs | Yes | No | ~10 min |
| `make refresh` | `export` + `import` | Yes | No | ~50 min |
| `make files` | Download Attachment / ContentVersion / Document / SDocs / Chatter binaries | Yes | **Yes (skip existing)** | 1–5 h first run, minutes on repeat |
| `make files-force` | Same as `files` but re-download everything | Yes | No (ignores disk) | same as first `files` run |
| `make files-gaps` | Only the "external" refs: SDocs PDFs + Chatter + ContentAsset | Yes | Yes | 10–60 min |
| `make files-audit` | Compare DB vs disk vs external refs | Read-only | — | seconds |
| `make metadata` | Tooling API: Flow, ValidationRule, Workflow, LWC, CustomField, CronTrigger, InstalledPackage | Yes | **Partially** (see below) | ~20 min first run, seconds on repeat |
| `make sfdx-retrieve` | Metadata API XML: Layouts, Profiles, CustomLabels, Settings, … | Yes | **No — full overwrite** (`--ignore-conflicts`) | 10–30 min |
| `make event-logs` | EventLogFile binaries (needs licence) | Yes | Yes | seconds–minutes |
| `make describe` | Adds human-readable object descriptions to `_sf_objects` table | Yes | Additive | seconds |
| `make browser` | Start the Flask UI on port 5003 | — | — | — |

---

## Idempotence vs. incremental — what actually happens on re-run

**Idempotent** = running the command twice produces the same final state.
Every command here is idempotent.

**Incremental** = the second run is materially cheaper because it only
fetches changes. Not all commands support this:

### Full refresh every time (no incremental)

- `make export`, `make refresh` — always queries every record of every
  object from Salesforce. A record deleted in the org will disappear from
  the CSV on the next run (which is usually what you want for a backup).
- `make import` — drops and recreates tables in `salesforce_full.db`. This
  is by design: a partial / additive rebuild risks leaving dangling rows
  from previous runs.
- `make sfdx-retrieve` — SFDX overwrites the retrieve target every time.
  You can't ask it for "only changed components."

### True incremental (first run slow, repeats fast)

- `make files` — per-file `os.path.exists` check. A file already on disk
  is skipped outright. The `manifest.json` is extended with new entries.
  **Caveat:** if a file's binary changed in Salesforce but the Id stayed
  the same, we won't re-fetch it. Use `make files-force` if you need to
  refresh known-stale files.
- `make files-gaps` — same pattern, restricted to external refs.
- `make event-logs` — each `EventLogFile` row has a unique Id, and once
  downloaded we skip it.

### Semi-incremental (depends on what changed)

- `make metadata`
  - **Flow versions** are immutable in Salesforce: editing a flow creates a
    new `Flow` record with a fresh Id and `VersionNumber`. Resume skips
    old versions and picks up new ones. Effectively incremental.
  - **ValidationRule, WorkflowRule, WorkflowFieldUpdate, WorkflowTask,
    WorkflowOutboundMessage, CustomField** are mutable: editing the rule
    keeps the same Id. Resume therefore skips _updated_ definitions. To
    capture edits, delete the relevant folder (or all of
    `data/sf_metadata/`) and re-run.
  - **LWC**: bundles and resources are updated in place. Same caveat as
    ValidationRule — resume misses edits. Delete
    `data/sf_metadata/LightningComponentBundle/<name>/` before re-run to
    force a refresh of that bundle.

If you need a definitive "everything is current" run before shutdown,
wipe `data/sf_metadata/` first and rerun `make metadata`. Same logic
applies to `data/sf_sfdx_metadata/` — but SFDX overwrites anyway, so
deleting is not strictly required there.

---

## Typical schedules

### Countdown backup (org is being deprecated)

Example: today is day −15, shutdown is day 0.

| When | Command | Purpose |
|---|---|---|
| Day −15 (today) | `make archive` | First complete snapshot; uncovers any gaps early |
| Day −15 | `make archive-audit` | Confirm every section says ✓ |
| Every 3–4 days | `make archive` | Catch new records, new Flow versions, new files |
| Last 48 h | Freeze org changes if possible; ask team to stop writing | Gives a clean final snapshot |
| Day −1 | `rm -rf data/sf_metadata && make archive` | Force-refresh mutable metadata to catch last-minute edits |
| Day −1 | `make archive-audit` | Final verification — gaps detected = still time to fix |
| Day −1 | Copy `data/` to external backup (SSD / S3 / tar) | Laptop failure insurance |
| Day 0 morning | `make export && make import` if org still live | Capture any overnight activity |

### Daily operational backup (org is staying, you just want a local copy)

```bash
# Once, at setup:
make archive

# Daily (e.g. cron at 02:00):
make export && make import    # fresh data
make files                    # new files only
make metadata                 # new flow versions
```

Takes 45–60 minutes on a mid-size org. Skip `sfdx-retrieve` and
`event-logs` if you don't care about layout / profile edits.

### Time-sensitive exceptions

- **Event Monitoring logs** (if you have the licence): retention is
  hourly/daily. If you want more than yesterday, run `make event-logs`
  **daily**, separately from the rest.
- **SetupAuditTrail**: 180-day retention, queried as part of `make export`.
  Running once a month is enough unless you expect admin-change audits
  soon.

---

## Troubleshooting

### `make files` 404s on ContentVersion URLs

```
! [342] 0681t000009Gb1TAAS: 404 Client Error: Not Found for url: .../VersionData
```

Harmless. The `ContentVersion` record exists in Salesforce but the binary
body was deleted (common with SDocs temp files). `make files-audit` will
list these as "missing on disk" — also harmless; nothing to do.

### `make sfdx-retrieve` "RetrieveTargetDirOverlaps"

Fixed: `scripts/sfdx_retrieve.sh` no longer passes `--output-dir` because
our `sfdx-project.json` already points the default package directory at
`data/sf_sfdx_metadata`. If you see this again, check that `sfdx-project.json`
in the repo root hasn't been edited to add a second `packageDirectories`
entry that conflicts.

### `make event-logs` returns 0 rows

```
  NOTE: no EventLogFile rows returned.
  Likely cause: Event Monitoring licence not enabled …
```

Expected if the org has no Event Monitoring licence. Without it, retention
is 1 day and most EventTypes don't record data for standard users. There's
nothing to recover — move on.

### `make metadata` "single-record restriction" error

Some Tooling sObjects (`Flow.Metadata`, `ValidationRule.Metadata`, …) can
only be queried **one record at a time**. The scripts already handle this
with the per-Id pattern. If you see a restriction error, it usually means
a new Salesforce API version changed the rule — check Salesforce Release
Notes and adapt the SOQL in `scripts/export_metadata.py`.

### `make sfdx-retrieve` hangs or times out

Large orgs (thousands of Layouts + Profiles) can exceed the default
`--wait 60` minutes. Edit `scripts/sfdx_retrieve.sh` and bump `--wait` to
120 or 240. Alternatively run `sf project retrieve start` manually with
a smaller `--manifest` (split `config/sfdx_package.xml` into two files —
one for Profile/PermissionSet/Layout, one for everything else).

### Salesforce session expires mid-`make archive`

`simple-salesforce` normally re-authenticates on the next call. If you see
`INVALID_SESSION_ID`, rerun the command that failed — thanks to resume
semantics you won't lose completed work.

---

## Pre-shutdown checklist

Before the org goes dark:

- [ ] `make archive-audit` prints `VERDICT: ✓`
- [ ] `data/salesforce_full.db` mtime is within the last 24 hours
- [ ] `data/sf_metadata/Flow/` has roughly one JSON per active flow in
      Setup → Flows, plus older inactive versions
- [ ] `data/sf_sfdx_metadata/main/default/layouts/` is non-empty
      (if SFDX retrieve succeeded)
- [ ] `data/sf_files/manifest.json` lists attachments you expect (spot-check
      a few SDocs PDFs)
- [ ] External backup of `data/` exists (not just on the laptop running
      the export). A `tar czf sfdc-archive.tgz data/` + upload to S3 /
      GCS / external SSD is enough.
- [ ] Document manual items that the scripts can't capture:
  - Certificates exported from Setup → Certificate and Key Management
    (only needed if SAML SSO / mutual-TLS endpoints depend on them)
  - SSO / IdP configuration screenshots
  - IP whitelists and login hours per Profile
  - Any third-party integrations that depend on org-specific URLs
    (`https://<org>.my.salesforce.com/<Id>`) — these will break when the
    org is shut down, regardless of this backup.

Non-recoverable after shutdown (no amount of tooling fixes these):

- Shield Platform Encryption tenant secrets — if any field is
  Shield-encrypted, export decrypted copies **before** shutdown.
- Managed-package Apex source code from AppExchange apps — not retrievable
  via any API.
- Historical SetupAuditTrail older than 180 days (already gone today).
- Field history older than 18 months without a Shield licence.
- EventLogFile older than 1 day without an Event Monitoring licence.
- Debug logs (`ApexLog`) older than ~20 days.

Keep this list with the backup — it's the definitive "what you can't
restore" record.

---

## Further reading

- `docs/architecture.md` — design of the export and import pipelines
- `README.md` — quickstart, philosophy, what the browser shows
- `scripts/audit_archive.py` — implementation of the audit that backs
  `make archive-audit`
