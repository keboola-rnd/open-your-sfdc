# open-your-sfdc

**Liberate your Salesforce data. Own it locally. Work with it agentically.**

A self-contained toolkit that pulls your entire Salesforce org into a local
SQLite database and a folder of binary files — then hands the whole thing to
**you and your AI agents** to build on. Browse it, query it, write your own
MCP servers, your own CLIs, your own guardrails. No more waiting for IT to
open an API, no more per-seat licensing on your own data, no more worrying
about what happens when the org goes dark.

## Why this exists

In April 2026 Marc Benioff announced **Salesforce Headless 360**:

> *"No Browser Required! Our API is the UI. Entire Salesforce & Agentforce
> & Slack platforms are now exposed as APIs, MCP, & CLI. All AI agents can
> access data, workflows, and tasks directly in Slack, Voice, or anywhere
> else with Salesforce Headless 360. Faster builds, agentic everything."*

The reply that went viral got it right:

> *"the api is the easy part. the hard part is least-privilege scoping,
> per-call audit trails, and revocable access for agents hitting your core
> data. that's the actual enterprise bottleneck, not the headless layer."*
> — @bytecrafter_1

**Exactly.** An API that lets an agent touch production CRM is a liability
until you've solved scoping, auditing, and kill-switches. And even then you
are renting access to your own data, on someone else's roadmap, priced per
agent call.

`open-your-sfdc` takes the other path:

1. **Extract once.** Dump the whole org — records, files, metadata — to a
   local SQLite file and a folder of binaries you fully control.
2. **Own it.** The `.db` file *is* your data. No tenant, no token, no
   rate limit. Copy it, version it, diff it, back it up to S3, hand a
   read-only replica to an agent.
3. **Build agentically.** Point Claude Code (or any coding agent) at the
   local DB and let it write your MCP servers, your migration scripts,
   your per-record guardrails, your custom views, your tests — all against
   a real schema with real data, with zero risk to the live org.

The strongest form of least-privilege scoping is an **offline SQLite file
on your laptop**. The strongest audit trail is `git log` over the queries
your agents ran. The strongest kill-switch is `rm data/salesforce_full.db`.

## What you get out of the box

- **Export.** Auto-discovers every queryable SObject in your org, downloads
  all records (REST `query_all` for small tables, Bulk API for big ones),
  writes one CSV per object plus a `metadata.json` describing every field.
- **Import.** Auto-generates a SQLite schema — typed columns, indexes on
  every reference field, FTS5 full-text indexes on text columns. One `.db`
  file holds your whole org.
- **Files.** Downloads the actual binaries referenced from the DB: classic
  Attachments, Lightning ContentVersions, Documents, Chatter FeedAttachments,
  and SDocs-generated PDFs (invoices / quotes / contracts that would
  otherwise be invisible to a normal export).
- **Browse.** A Flask web app on `http://localhost:5003` with a reference
  **Keboola view** for reading like a business user, a **Raw view** for
  forensics, a unified activity timeline (Task + Event + EmailMessage), a
  Files panel with inline previews, and a Migration Report that generates
  PostgreSQL DDL + selective CSV exports.

## What you build on top (this is the point)

The browser and the two default views are a **starter template**, not the
product. The product is the local `.db` file and your agent loop on top of
it. Open the repo in Claude Code and ask for things like:

- *"Add a new view called `deal-review` that shows every Opportunity over
  $50k with its last three Tasks, linked Contacts, and attached PDFs."*
- *"Write an MCP server that exposes three tools: `find_account(name)`,
  `recent_activity(account_id, days)`, and `related_files(record_id)` —
  backed by the SQLite DB, read-only, with a 100-row cap."*
- *"Generate a CLI that finds duplicate Accounts by fuzzy name match and
  writes the merge plan to `out/merges.csv` without touching the DB."*
- *"Add a guardrail: any tool call that returns PII columns
  (`Email`, `Phone`, `SSN__c`) must log the query, the caller, and the
  row count to `logs/pii_access.jsonl`."*
- *"Write pytest cases that assert every `Account.OwnerId` resolves to a
  real `User.Id` in the current snapshot."*

Each of those is a 5-to-30-minute task with a coding agent against a real
schema and real data. The same tasks against a live org mean tickets,
scoped OAuth apps, sandbox refreshes, and a sinking feeling when something
writes to prod. Locally, the worst case is you re-run `make refresh`.

The **Keboola view** is itself just an example of this workflow — a
Lightning-inspired layout one of us built in an afternoon. Yours can be
completely different. Copy it, fork it, or throw it away and write a
`sales-ops view`, an `audit view`, a `migration view`. That's the whole
idea.

## Quickstart

```bash
git clone https://github.com/keboola-rnd/open-your-sfdc && cd open-your-sfdc
make install
cp .env.example .env   # fill in SF credentials

make refresh           # export to CSV + import to SQLite (~5-30 minutes)
make files             # download binaries (can take hours for big orgs)
make files-gaps        # fetch SDocs PDFs + chatter attachments the default
                       # scan skipped (permissions, external refs, etc.)

make browser           # http://localhost:5003
```

All outputs land under `data/` which is gitignored. After `make refresh`
you are done with Salesforce — the rest of the workflow runs on your
laptop, against your file.

## Architecture

```
Salesforce org
      │
      │  scripts/export_all.py         (describe + query_all / bulk API)
      ▼
data/sf_full_export/*.csv + metadata.json
      │
      │  scripts/import_to_sqlite.py   (auto-schema, indexes, FTS5)
      ▼
data/salesforce_full.db  +  data/sf_files/  (binaries via download_files.py)
      │
      ├─► run.py → sf_browser/ (Flask, port 5003)        ← reference UI
      │
      └─► your MCP server / CLI / tests / guardrails /   ← the point
          custom views / migration pipeline / agents
          (built with Claude Code against the local DB)
```

## Commands

Run `make help` for the full menu. Highlights:

| Target | Script | Purpose |
|---|---|---|
| `make export` | `scripts/export_all.py` | Dump every SObject to CSV |
| `make export-dry` | `scripts/export_all.py --dry-run` | Show what would run |
| `make import` | `scripts/import_to_sqlite.py` | CSV → SQLite (drops + rebuilds) |
| `make refresh` | both | Export + import in one go |
| `make describe` | `scripts/fetch_descriptions.py` | Adds human object descriptions |
| `make files` | `scripts/download_files.py` | Download attachments & content bodies |
| `make files-dry` | `… --dry-run` | What would be downloaded |
| `make files-audit` | `scripts/audit_files.py` | Compare DB vs disk vs external refs |
| `make files-gaps` | `… --type external --resume` | SDocs PDFs + chatter refs |
| `make browser` | `run.py` | Start the reference web UI on port 5003 |
| `make browser-stop` | | Kill anything listening on 5003 |

## Keboola view vs Raw view (both are just reference views)

- **Keboola view** (default): business-friendly Lightning-inspired layout —
  highlights strip, grouped sections (Company / Financials / Invoicing /
  Contact info / Audit / …), related-list previews with resolved lookup
  names, activity timeline, files panel. Named after the team that wrote
  it; not load-bearing. Fork it.
- **Raw view**: unstyled dump of every field (priority fields, populated,
  empty) and every inbound/outbound relationship. Best when the Keboola
  heuristics hide something you need to see.

Every record page has a toggle. Writing a third view is a Claude Code
task, not a PR to this repo (unless you want it to be).

## Files panel

For each record, the browser surfaces every binary attached to it:

- Classic **Attachments** (`ParentId = record`)
- Lightning **ContentVersions** linked via `ContentDocumentLink`
- **SDocs-generated PDFs** (invoices, quotes, contracts) via
  `SDOC__SDoc__c.SDOC__ObjectID__c`

PDFs and images preview inline; anything else downloads with its original
filename. Binaries that are referenced in the DB but haven't been
downloaded yet show up with a "not downloaded" badge so you know they
exist.

## Migration Report

`/migrate` lists every exported object with:

- Record count and field count
- Per-field **fill rate**, distinct value count, top 5 values
- Auto-generated PostgreSQL **`CREATE TABLE`** DDL (pick which columns to
  include)
- Streaming CSV export with only the columns you selected

Designed for the inevitable *"OK we're moving to a real database — which
fields do we actually need?"* conversation, which is also the conversation
that tends to happen right before *"and can an agent do this for us?"*

## The philosophy

> Your CRM data is yours. Act like it.

Salesforce is a great system of record. It is a terrible system of
**agency**. Headless 360 does not fix that — it just charges you more to
keep your agents inside the perimeter. The way out is the same way out
teams have always chosen when a SaaS vendor started squeezing: *get a
copy, own the copy, build on the copy*.

Once the copy is a plain SQLite file next to a folder of binaries, every
capability Salesforce wants to sell you (agents, MCP, CLI, Slack
integration) becomes a weekend project with Claude Code. Least-privilege
scoping is `SELECT` on a read-only view. Audit trails are `logs/`.
Revocable access is `chmod 000`.

That is what this repo is for.

## Credits

Extracted from the `finance_invoicing` monorepo at Keboola R&D. The tool
was born out of a real Salesforce off-boarding project and hardened
against the long tail of edge cases — compound fields, polymorphic
references, SOQL length limits, Bulk API quirks, FeedAttachment
permissioning, SDocs external files, filename collisions, and so on.
