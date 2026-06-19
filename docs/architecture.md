# Universal Salesforce Export & Data Browser

## Goal

Export **all data** from our Salesforce org into a local SQLite database and provide
a web-based browser for exploring it. This replaces the current piecemeal approach
(6 billing objects + 3 CRM objects) with a single, comprehensive export.

---

## Current State

### What we have today

| Script | Objects | Output |
|--------|---------|--------|
| `scripts/sf_connect.py` | Salesforce auth (`get_connection`) and `flatten_record` helper | — |
| `scripts/export_all.py` | Every queryable SObject in the org | `data/sf_full_export/*.csv` |
| `scripts/import_to_sqlite.py` | All exported objects | `data/salesforce_full.db` |
| `scripts/download_files.py` | Attachments, ContentVersion, Documents, SDocs PDFs | `data/sf_files/` |

**Reusable infrastructure:**
- `get_connection()` -- Salesforce auth via `.env` + `simple_salesforce`
- `flatten_record()` -- flattens nested SF objects to flat dicts
- `import_to_sqlite.py` -- CSV -> SQLite with type mapping and batch inserts
- `data/sf_full_export/metadata.json` -- complete field metadata for every exported object

### What is missing

- ~90% of Salesforce objects are not being downloaded
- Even for downloaded objects, only selected columns are fetched
- No Contact, Lead, Campaign, Task, Event, Case, Contract, Quote, User, etc.
- No way to browse the data interactively beyond the specialized invoicing/bookings apps

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Salesforce Org                        │
│  (Accounts, Contacts, Opportunities, Orders, Cases,     │
│   Campaigns, Tasks, Events, Custom Objects, ...)        │
└──────────────────────┬──────────────────────────────────┘
                       │
           Step 1: Universal Export Script
           (sf.describe() -> discover all objects)
           (sf.Object.describe() -> all fields per object)
           (query_all / bulk API -> download records)
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              data/sf_full_export/                        │
│  ├── metadata.json        (all objects + fields schema) │
│  ├── Account.csv          (all fields, all records)     │
│  ├── Contact.csv                                        │
│  ├── Opportunity.csv                                    │
│  ├── Order.csv                                          │
│  ├── Invoice__c.csv                                     │
│  ├── ... (every queryable object)                       │
│  └── _export_log.json     (stats, errors, timestamps)   │
└──────────────────────┬──────────────────────────────────┘
                       │
           Step 2: Universal SQLite Import
           (auto-create tables from metadata)
           (type mapping: SF types -> SQLite types)
           (auto-create indexes on Id, foreign keys)
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              data/salesforce_full.db                     │
│  (one table per SF object, all columns, all records)    │
│  (plus _metadata table with schema info)                │
│  (plus _relationships table with FK mappings)           │
└──────────────────────┬──────────────────────────────────┘
                       │
           Step 3: Web Data Browser (Flask)
           (generic table browser, no hardcoded objects)
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              SF Data Browser (port 5003)                 │
│                                                         │
│  / ................... Dashboard (object list + stats)   │
│  /table/<name> ........ Table view (paginated, sorted)  │
│  /table/<name>/<id> ... Record detail + relationships   │
│  /search .............. Global full-text search          │
│  /query ............... SQL query editor                 │
│  /schema/<name> ....... Object schema browser           │
│  /api/... ............. JSON API for every view          │
└─────────────────────────────────────────────────────────┘
```

---

## File → record link layer

Binary files (signed order PDFs, contracts, invoices) are only useful if you
can answer *"which Order / Opportunity / Account is this attached to?"*. Two
mechanisms keep that join complete:

1. **Export side** (`export_all.py`) — `ContentVersion` and `ContentDocument`
   are silently scoped by the Salesforce Files query-planner to a ~30-row
   sample on an unfiltered `SELECT`. We force the full set via `MANDATORY_WHERE`
   (`ContentVersion WHERE IsLatest = true`) and derive `ContentDocument` from
   it, which lets the existing `FILTER_REQUIRED_OBJECTS` machinery rebuild the
   full `ContentDocumentLink` (every `LinkedEntityId ↔ ContentDocumentId`). The
   chain is pinned to export ContentVersion → ContentDocument →
   ContentDocumentLink (`FILES_EXPORT_ORDER`), since each is derived from the
   previous one's CSV.

2. **Manifest side** (`download_files.py`) — every `sf_files/manifest.json`
   entry is enriched with a typed `linked_entity_id` / `linked_entity_type`,
   resolved in precedence order from `ContentDocumentLink` →
   `FirstPublishLocationId` → DocuSign/SDoc parent id. `content_document_id` is
   kept to the real `069…` id only (no longer overloaded with parent/envelope
   ids).

This is what recovers DocuSign signed-PDF → Order links: DocuSign writes the
completed PDF back as a normal `ContentVersion` linked to the Order via
`ContentDocumentLink`, which the export now captures in full. See
[enhancement-file-to-record-links.md](enhancement-file-to-record-links.md).

---

## Phase 1: Universal Export Script

**New file:** `scripts/export_all.py`

### 1.1 Object Discovery

```python
# Get all objects in the org
all_objects = sf.describe()["sobjects"]

# Filter to queryable, non-system objects
exportable = [
    obj for obj in all_objects
    if obj["queryable"]
    and not obj["name"].endswith("__History")
    and not obj["name"].endswith("__Feed")
    and not obj["name"].endswith("__Share")
    and not obj["name"].endswith("__Tag")
    and not obj["name"].endswith("__ChangeEvent")
    and obj["name"] not in SKIP_OBJECTS
]
```

**Skip list** (system/metadata objects that fail or return useless data):
- `*__History`, `*__Feed`, `*__Share`, `*__Tag`, `*__ChangeEvent` -- auto-generated
- `IdeaComment`, `Vote`, `ContentBody` -- special handling required
- `EventLogFile`, `ApexLog` -- operational logs, not business data
- `SetupAuditTrail` -- admin audit, not business data
- Objects where `retrieveable == False`

### 1.2 Field Discovery

For each object, get all fields and filter out compound fields (Address, Location)
that cannot be selected directly in SOQL:

```python
desc = getattr(sf, object_name).describe()
fields = [
    f["name"] for f in desc["fields"]
    if f["type"] not in ("address", "location")
]
```

### 1.3 Download Strategy

| Record count | Method | Rationale |
|-------------|--------|-----------|
| < 50,000 | `sf.query_all()` | Simple, reliable, handles pagination |
| >= 50,000 | `sf.bulk.Object.query_all()` | Memory-efficient, faster for large sets |

**Pre-check record count:**
```python
count_result = sf.query(f"SELECT COUNT() FROM {object_name}")
record_count = count_result["totalSize"]
```

**Rate limiting:**
- Check limits before starting: `sf.restful("limits")`
- Sleep between objects (configurable, default 1 second)
- Exponential backoff on errors (429, 503)
- Log all API usage for monitoring

### 1.4 Output Format

**CSV files** in `data/sf_full_export/<ObjectName>.csv`:
- One file per object
- All fields included
- UTF-8 encoding
- Standard CSV quoting

**Metadata** in `data/sf_full_export/metadata.json`:
```json
{
  "export_date": "2026-04-16T10:30:00Z",
  "sf_instance": "keboola.my.salesforce.com",
  "objects": {
    "Account": {
      "label": "Account",
      "record_count": 1234,
      "fields": [
        {
          "name": "Id",
          "label": "Account ID",
          "type": "id",
          "referenceTo": []
        },
        {
          "name": "Name",
          "label": "Account Name",
          "type": "string",
          "length": 255
        },
        {
          "name": "OwnerId",
          "label": "Owner ID",
          "type": "reference",
          "referenceTo": ["User"]
        }
      ]
    }
  }
}
```

**Export log** in `data/sf_full_export/_export_log.json`:
```json
{
  "started_at": "2026-04-16T10:30:00Z",
  "finished_at": "2026-04-16T10:45:00Z",
  "total_objects": 85,
  "total_records": 234567,
  "objects": {
    "Account": {"status": "ok", "records": 1234, "duration_sec": 3.2},
    "SomeObject": {"status": "error", "error": "QUERY_TIMEOUT", "records": 0}
  },
  "api_calls_used": 342,
  "errors": ["SomeObject: QUERY_TIMEOUT"]
}
```

### 1.5 CLI Interface

```bash
# Export everything
python scripts/export_all.py

# Export specific objects only
python scripts/export_all.py --objects Account,Contact,Opportunity

# Export only custom objects
python scripts/export_all.py --custom-only

# Dry run -- show what would be exported
python scripts/export_all.py --dry-run

# Resume after failure (skip already exported objects)
python scripts/export_all.py --resume

# Include deleted records
python scripts/export_all.py --include-deleted
```

---

## Phase 2: Universal SQLite Import

**New file:** `scripts/import_to_sqlite.py`

### 2.1 Auto-Schema Generation

Read `metadata.json` and generate CREATE TABLE statements automatically:

```python
SF_TO_SQLITE = {
    "id": "TEXT",
    "reference": "TEXT",
    "string": "TEXT",
    "textarea": "TEXT",
    "picklist": "TEXT",
    "multipicklist": "TEXT",
    "boolean": "INTEGER",
    "int": "INTEGER",
    "double": "REAL",
    "currency": "REAL",
    "percent": "REAL",
    "date": "TEXT",       # ISO format
    "datetime": "TEXT",   # ISO format
    "time": "TEXT",
    "email": "TEXT",
    "phone": "TEXT",
    "url": "TEXT",
    "base64": "BLOB",
    "anyType": "TEXT",
}
```

### 2.2 Auto-Index Creation

- Primary key index on `Id` column (every SF object has one)
- Indexes on all `reference` type fields (foreign keys)
- Full-text search index on key text fields per object

### 2.3 Metadata Tables

**`_sf_objects`** -- object catalog:
```sql
CREATE TABLE _sf_objects (
    name TEXT PRIMARY KEY,
    label TEXT,
    custom INTEGER,
    record_count INTEGER,
    exported_at TEXT
);
```

**`_sf_fields`** -- field catalog:
```sql
CREATE TABLE _sf_fields (
    object_name TEXT,
    field_name TEXT,
    label TEXT,
    sf_type TEXT,
    sqlite_type TEXT,
    length INTEGER,
    reference_to TEXT,  -- comma-separated target objects
    nillable INTEGER,
    PRIMARY KEY (object_name, field_name)
);
```

**`_sf_relationships`** -- precomputed relationship map:
```sql
CREATE TABLE _sf_relationships (
    from_object TEXT,
    from_field TEXT,
    to_object TEXT,
    relationship_name TEXT
);
```

### 2.4 CLI Interface

```bash
# Import everything
python scripts/import_to_sqlite.py

# Target specific DB path
python scripts/import_to_sqlite.py --db data/my_export.db

# Skip FTS index creation (faster import)
python scripts/import_to_sqlite.py --no-fts
```

---

## Phase 3: Web Data Browser

**App:** `sf_browser/` (served by `run.py` on port 5003)

### 3.1 Tech Stack

- **Flask** -- lightweight web framework
- **SQLite** -- read-only access to `salesforce_full.db`
- **Jinja2 + Tailwind CSS** -- templates with modern styling
- **HTMX** -- interactive UI without heavy JS framework
- No external database dependencies (everything in SQLite)

### 3.2 Pages

#### Dashboard (`/`)
- Total objects count, total records count, export date
- Object list as cards: name, label, record count, custom flag
- Search/filter objects by name
- Quick links to most populated objects

#### Table View (`/table/<object_name>`)
- Paginated table (configurable page size, default 50)
- Column sorting (click header to sort)
- Column filtering (type-aware: text search, numeric range, date range, picklist)
- Column visibility toggle (hide/show columns)
- Reference fields rendered as clickable links to target records
- Export current view as CSV
- Record count and page navigation

#### Record Detail (`/table/<object_name>/<record_id>`)
- All fields displayed in a clean layout
- Field labels from metadata (not raw API names)
- Reference fields as links to related records
- "Related Lists" section: all objects that reference this record
  (e.g., viewing an Account shows its Contacts, Opportunities, Orders, etc.)

#### Schema Browser (`/schema/<object_name>`)
- Complete field list with types, labels, lengths
- Relationship diagram (which objects reference this one)
- Custom vs. standard field indicators

#### SQL Query Editor (`/query`)
- Text area for free-form SQL
- Execute and display results as table
- Query history (stored in browser localStorage)
- Pre-built example queries

#### Global Search (`/search`)
- Search across all objects by text
- Uses FTS indexes for fast results
- Results grouped by object type

### 3.3 API Endpoints

Every page has a corresponding JSON API endpoint:

| Page | HTML | JSON API |
|------|------|----------|
| Dashboard | `GET /` | `GET /api/objects` |
| Table view | `GET /table/<name>` | `GET /api/table/<name>?page=1&sort=Name&filter=...` |
| Record | `GET /table/<name>/<id>` | `GET /api/table/<name>/<id>` |
| Schema | `GET /schema/<name>` | `GET /api/schema/<name>` |
| Search | `GET /search?q=...` | `GET /api/search?q=...` |
| SQL Query | `GET /query` | `POST /api/query` (body: `{"sql": "..."}`) |

### 3.4 File Structure

```
open-your-sfdc/
├── sf_browser/
│   ├── __init__.py
│   ├── app.py              # Flask app factory
│   ├── routes.py           # All routes (HTML + API)
│   ├── database.py         # SQLite connection + query helpers
│   ├── filters.py          # Jinja2 template filters
│   ├── activity.py         # Activity timeline (Task/Event/Email)
│   ├── layout.py           # Keboola view section rules
│   ├── migration.py        # Migration report + DDL generator
│   └── files.py            # Binary files resolver
├── templates/              # Jinja2 HTML templates
├── static/                 # Custom CSS
├── scripts/                # Export / import / download CLIs
├── run.py                  # Entry point (port 5003)
└── requirements.txt        # Flask + simple-salesforce + ...
```

---

## Makefile Targets

```makefile
# Universal Salesforce Export
export: ## Export all Salesforce data to CSV
	.venv/bin/python scripts/export_all.py

export-dry: ## Dry run - show what would be exported
	.venv/bin/python scripts/export_all.py --dry-run

refresh: ## Export all data and import to SQLite
	.venv/bin/python scripts/export_all.py
	.venv/bin/python scripts/import_to_sqlite.py

# SF Data Browser
browser: ## Start Salesforce data browser (port 5003)
	.venv/bin/python run.py
```

---

## Implementation Order

### Step 1: Export Script (~Phase 1)
1. Object discovery with `sf.describe()`
2. Field discovery with per-object `describe()`
3. Record count pre-check
4. Download with `query_all()` (standard) / `bulk` (large objects)
5. Metadata and export log generation
6. CLI arguments (--dry-run, --objects, --resume, etc.)
7. Error handling and rate limiting

### Step 2: SQLite Import (~Phase 2)
1. Read metadata.json for schema generation
2. Auto-create tables with correct types
3. Import CSV files with type conversion
4. Create indexes (PKs, FKs, FTS)
5. Populate metadata tables (_sf_objects, _sf_fields, _sf_relationships)

### Step 3: Web Browser (~Phase 3)
1. Flask app factory + database connection
2. Dashboard page (object list)
3. Table view (pagination, sorting)
4. Record detail (all fields + related lists)
5. Schema browser
6. SQL query editor
7. Global search (FTS)
8. JSON API endpoints
9. Styling with Tailwind CSS + HTMX interactivity

---

## Sizing Estimates

**Salesforce org (expected):**
- ~80-120 queryable objects (standard + custom)
- ~500-2000 records for most objects
- ~10,000-50,000 for Orders, OrderItems, Invoices
- Total: likely 100,000-500,000 records across all objects

**Export time:** 5-15 minutes (depending on API limits and object count)

**SQLite database size:** 50-200 MB (estimated)

**Web browser performance:** SQLite handles millions of rows with proper indexes;
the browser should be snappy for our data volumes.

---

## Compatibility

- Existing scripts stay untouched in the source monorepo (this tool is a standalone extract)
- Existing apps (invoicing, bookings) continue using `data/salesforce.db`
- New universal export creates separate files:
  - `data/sf_full_export/` (CSV files + metadata)
  - `data/salesforce_full.db` (universal SQLite database)
- No conflicts with existing data pipeline

---

## Dependencies

No new Python packages required beyond what is already installed:
- `simple_salesforce` -- already used for SF connection
- `flask` -- already used for invoicing/bookings apps
- `sqlite3` -- Python standard library

Optional (for enhanced browser):
- `markupsafe` -- already installed with Flask (Jinja2 dep)
