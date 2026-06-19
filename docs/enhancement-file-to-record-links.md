# Enhancement spec: export file → record links (ContentDocumentLink) completely

**Status:** implemented (branch `feat/file-to-record-links`) — pending a live
re-export to confirm row-count convergence (see §11)
**Author:** prepared 2026-06-19 (diagnosis session against `data-2026-06-16`)
**Owner:** implemented 2026-06-19
**Repo:** `keboola-rnd/open-your-sfdc`

---

## 1. Why this exists (motivation)

Downstream goal: import **signed order PDFs** (and other order/opportunity
documents) from a Salesforce archive into the Keboola CRM, attaching each
document to the correct Order via
`crm files upload --parent-type order --parent-id <sfid> --document-type signed_order`.

That import is **currently impossible** because the archive does not record
**which file belongs to which business record**. The binaries are all on disk,
but the file→record link is missing for ~99.7 % of them.

This spec describes the gap, its two root causes, and the changes needed so a
future archive can be imported.

## 2. Problem statement (measured on `data-2026-06-16`)

| Fact | Value |
|---|---|
| Files downloaded (manifest entries) | **10 182** (9 280 PDF + 615 docx + …) |
| Distinct `ContentDocument`/source IDs in manifest | 8 606 |
| `ContentDocumentLink` rows exported (SQLite **and** CSV) | **62** |
| …of those linked to an **Order** (`801…`) | **1** (and it is an `image.png`) |
| …linked to an **Opportunity** (`006…`) | **0** |
| `ContentVersion` rows in `sf_full_export/ContentVersion.csv` | **30** |
| …all with `FirstPublishLocationId` prefix | `005` (User) only |
| `Attachment` rows linked to an Order | **0** |

Net effect: of 10 182 archived files, only the 30 that happen to be **owned by a
User** carry any link metadata. Every order-form / signed-contract / SOW PDF
that was attached to an Order, Opportunity, or Account has its bytes on disk but
**no recoverable pointer to the record it belongs to**.

A consumer therefore has to guess by filename (e.g. account name in the title),
which is unreliable — generic tokens collide (`…Master **Service** Agreement`,
`Publicis **Group**`) and would risk attaching one customer's legal document to
another customer's order.

## 3. Root-cause analysis

There are **two independent gaps**, both in how the *file-link* layer is built.
The *binary download* layer already works (it pulls all 10 k files).

### Cause A — `ContentDocumentLink` is derived from a truncated parent set

`scripts/export_all.py` treats `ContentDocumentLink` as a filter-required object
(correct — SF rejects `SELECT … FROM ContentDocumentLink` without a `WHERE` on
`ContentDocumentId` or `LinkedEntityId`):

```python
# export_all.py
FILTER_REQUIRED_OBJECTS = {
    "ContentDocumentLink": ("ContentDocument", "ContentDocumentId", "Id", 200),
    ...
}
```

It loads parent IDs from the **`ContentDocument` CSV** and queries
`ContentDocumentLink WHERE ContentDocumentId IN (chunk)`. But the
`ContentDocument` export itself only produced **30 rows**, so the link query only
ever sees 30 documents → 62 links.

Why is `ContentDocument`/`ContentVersion` only 30 rows? `export_all.py` issues a
plain, unfiltered select for every object (`query_all_standard`, line ~243):

```python
soql = f"SELECT {', '.join(chunks[0])} FROM {object_name}"   # no WHERE
```

For `ContentVersion`/`ContentDocument`, an **unfiltered** select returns only the
records the running user directly owns/can see (SF Files sharing scoping) — here,
30 User-owned org assets (logos, icons). Proof it is a query problem, not a
permissions problem: `download_files.py` pulls **10 041** versions from the same
org with the same credentials, simply by adding a filter:

```python
# download_files.py (works)
soql = "... FROM ContentVersion WHERE IsLatest = true"   # → 10 041 rows
```

### Cause B — `download_files.py` downloads bytes but discards the entity link

`download_files.py` fetches all 10 041 latest `ContentVersion`s, but:

1. Its `ContentVersion` SOQL does **not** select `FirstPublishLocationId` (the
   record a file was first published to — a free, partial link).
2. The manifest entry it writes carries only
   `{type, content_document_id, title, extension, size, path, created_date}` —
   **no `LinkedEntityId`**.
3. The one place it *does* capture a record link is the DocuSign / S-Docs path:
   for those it deliberately overloads `content_document_id` with the **parent
   record id** (Order/Opp), see lines ~462-463:

   ```python
   # Group signed envelope artifacts under the parent record (Order/Opp).
   doc_id = docusign_md.get("parent_id") or docusign_md.get("envelope_id") or doc_id
   ```

   That is why ~515 manifest entries have a `content_document_id` with prefix
   `801` (Order) or `800` (Contract). It is useful but (a) **overloads one field
   with three different meanings** (real `069…` ContentDocument vs. S-Docs `a0A…`
   vs. DocuSign parent `801…`), and (b) only covers DocuSign/S-Docs envelopes,
   **not** files attached normally through the Files UI or DocuSign's
   **completed** PDF written back as a standard ContentVersion.

### Why "signed PDFs" specifically are missing

DocuSign-for-Salesforce (`dfsle`) stores only **source** documents in
`dfsle__Document__c` (1 267 `ContentVersion` + 322 `Template` rows — the
*unsigned* order forms sent for signature). When an envelope completes, DocuSign
writes the **completed/signed PDF back into Salesforce as a normal
`ContentVersion` linked to the parent Order via `ContentDocumentLink`**. That
completed PDF *is* among the 10 041 downloaded binaries — but its Order link
lived in `ContentDocumentLink`, which Cause A truncated to 30 documents. So the
signed PDF is on disk with its link severed.

## 4. Goal / desired outcome

A future archive must let a consumer answer, for any file:
**"which Order / Opportunity / Account / Contact is this attached to?"** — without
filename guessing.

Concretely, after `make archive` the export must contain a **complete
`ContentDocumentLink`** (every `LinkedEntityId` ↔ `ContentDocumentId`) covering
all downloaded documents, joinable to `ContentVersion` (→ `ContentDocumentId`)
and to the binary on disk (manifest `path`).

## 5. Proposed changes

Ordered by importance. R1+R2 are the must-haves; R3+R4 are cheap robustness.

### R1 (must) — Build `ContentDocumentLink` from the full document set

Stop sourcing parent IDs from the truncated `ContentDocument` CSV. Instead drive
the chunked `WHERE ContentDocumentId IN (...)` query from the **full set of
downloaded `ContentDocumentId`s** (8 606 distinct in the manifest; ~44 chunks of
200). Options, pick one:

- Have `download_files.py` emit the authoritative `ContentDocumentId` list (it
  already has every `ContentVersion.ContentDocumentId`), and have the link export
  consume that list; **or**
- Fix R2 first (so the `ContentDocument`/`ContentVersion` CSVs are complete) and
  the existing `FILTER_REQUIRED_OBJECTS` machinery in `export_all.py` will then
  see all documents automatically.

Either way, also fetch the **reverse** direction is not needed — `ContentDocumentId
IN` already returns every `LinkedEntityId` (Order, Opp, Account, …) for each doc.

### R2 (must) — Make `ContentVersion` / `ContentDocument` export complete

In `export_all.py`, the unfiltered `SELECT … FROM ContentVersion` returns 30 rows
due to Files sharing scope. Align it with the proven `download_files.py`
approach:

- Special-case `ContentVersion` to query `WHERE IsLatest = true` (matches the
  10 041 the downloader already retrieves), and
- Derive the `ContentDocument` set from those versions' `ContentDocumentId`s
  (rather than the plain `SELECT FROM ContentDocument`, which is scoped the same
  way).

Verify the row counts converge: `ContentVersion.csv` should hold ~10 k rows, not
30.

### R3 (should) — Capture `FirstPublishLocationId` as a cheap partial link

Add `FirstPublishLocationId` to the `ContentVersion` SOQL in `download_files.py`
(line ~77) and persist it in the manifest entry. For many order-form PDFs this
field alone already points at the Order/Opportunity, giving a fallback link that
doesn't depend on `ContentDocumentLink` visibility.

### R4 (should) — Stop overloading `content_document_id`; add explicit `linked_entity_id`

In the manifest, keep `content_document_id` meaning *only* the real `069…`
ContentDocument id, and add a separate, explicit field (e.g. `linked_entity_id`
+ `linked_entity_type`) populated from DocuSign/S-Docs `parent_id` and/or
`FirstPublishLocationId` and/or the new `ContentDocumentLink`. Document the
precedence. This removes the current three-meanings-in-one-field ambiguity that
makes the manifest hard to consume.

## 6. Acceptance criteria

1. `sf_full_export/ContentDocumentLink.csv` contains **thousands** of rows (not
   62), including links with `LinkedEntityId` prefixes `801` (Order), `006`
   (Opportunity), `001` (Account), `800`/`806` (Contract) where such links exist
   in the org.
2. `sf_full_export/ContentVersion.csv` row count matches the downloaded file
   count (~10 k), i.e. R2 closed the 30-vs-10 041 gap.
3. For a sampled set of **active Orders** that have documents in Salesforce, the
   export lets you resolve Order id → `ContentDocumentLink` → `ContentVersion` →
   on-disk `path`, and the resolved file is the expected order form / signed PDF.
4. The manifest exposes an explicit `linked_entity_id` (R4) for every file that
   has one, with no overloading of `content_document_id`.
5. README / `docs/architecture.md` updated to describe the file-link layer.

## 7. Verification plan

- Re-run `make files` + the link export against the live org; diff
  `ContentDocumentLink.csv` row count before/after.
- Pick 10 active Orders known to have signed PDFs (e.g. via DocuSign
  `dfsle__Envelope__c` whose `dfsle__SourceId__c` is an Order); confirm each
  resolves end-to-end to a binary on disk.
- Cross-check a few against the Salesforce UI (open the Order → Files related
  list) to confirm the link is correct, not just present.
- Guard against false coverage: `log` how many `ContentDocumentId`s returned
  **zero** links (deleted CV / no access) so truncation can't masquerade as
  completeness.

## 8. Risks & open questions

- **Governor limits / visibility:** `ContentDocumentLink` queried by
  `ContentDocumentId IN` returns only links the integration user can see. Confirm
  the export user has "View All Data" / adequate Files access, or some Order
  links will silently be missing. (The existing 30-row scoping is the warning
  sign.)
- **Confirm R2 mechanism:** validate that `WHERE IsLatest = true` is what lifts
  the 30→10 041 gap (vs. a permission/profile difference) before relying on it.
- **Volume:** ContentDocumentLink across 8 606 docs may be tens of thousands of
  rows; ensure chunked queries + CSV writing scale (they already do for other
  filter-required objects).
- **History/soft-deleted:** decide whether `IsDeleted` links/versions are needed
  for a decommissioning archive (`archive-shutdown` path).

## 9. Out of scope

- The CRM-side import itself (uploading via `crm files upload`) — separate work,
  blocked on this export.
- Re-pointing already-imported active Orders (which are mostly 2025-2026 renewals
  whose original signatures sit on older, now-`Finished` orders) — a data-mapping
  question for the import, not the export.
- Pulling completed PDFs directly from the DocuSign API (the SF write-back copy is
  sufficient once R1 restores its link).

## 10. Code pointers

| What | Where |
|---|---|
| Truncated link derivation | `scripts/export_all.py` → `FILTER_REQUIRED_OBJECTS` (~L133), `query_all_standard` (~L243), `fetch_filtered_records` (~L300-332) |
| Working full CV download | `scripts/download_files.py` → CV SOQL (~L77-82), manifest write (~L125-133) |
| DocuSign/S-Docs parent-id overload | `scripts/download_files.py` (~L300-353, L462-463) |
| Per-object export loop | `scripts/export_all.py` (~L441-471) |

> Note: at diagnosis time the working tree had **uncommitted changes** in
> `Makefile`, `scripts/download_files.py`, `scripts/export_all.py`,
> `scripts/export_metadata.py`. Reconcile those before implementing.

## 11. Implementation notes (2026-06-19)

Implemented on branch `feat/file-to-record-links` (off `main`, so the unrelated
uncommitted robustness changes noted above are deliberately excluded — they
remain a separate concern).

**Export side — `scripts/export_all.py`:**

- `MANDATORY_WHERE = {"ContentVersion": "IsLatest = true"}` is injected into
  `count_records`, `query_all_standard`, `query_all_bulk`, and the API fallback
  in `get_parent_ids`, so the unfiltered-scope problem is fixed everywhere a
  ContentVersion SELECT is built (R2).
- `FILTER_REQUIRED_OBJECTS` gains
  `ContentDocument: ("ContentVersion", "Id", "ContentDocumentId", 200)` —
  ContentDocument is rebuilt by chunking `WHERE Id IN (<ContentDocumentIds from
  the now-complete ContentVersion export>)` (R2), which automatically restores
  the full `ContentDocumentLink` through the existing machinery (R1).
- `FILES_EXPORT_ORDER` + `_pin_files_chain_order()` pin the export order to
  ContentVersion → ContentDocument → ContentDocumentLink regardless of the
  alphabetical sort, so each parent CSV exists before its child runs.

**Manifest side — `scripts/download_files.py`:**

- `download_content_versions` now selects `FirstPublishLocationId` and stores it
  on each entry (R3).
- New `enrich_manifest_with_record_links()` (run in `main()` before the manifest
  is saved) adds typed `linked_entity_id` / `linked_entity_type` /
  `linked_entity_source` (+ `linked_entities` when a doc is shared), resolved in
  precedence order ContentDocumentLink → FirstPublishLocationId →
  DocuSign/SDoc parent, and de-overloads `content_document_id` back to the real
  `069…` id or null (R4). Reads the SQLite DB read-only; degrades gracefully if
  it is absent (e.g. a standalone `download_files.py` run that skipped import).

**Verification done (offline):**

- Unit-tested `_pin_files_chain_order` (ordering + 0/1-member edge cases) and the
  enrich precedence + `content_document_id` de-overload against a synthetic
  SQLite DB.
- Smoke-ran enrich over the real 10,182-entry `data-2026-06-16` manifest + DB:
  no errors; post-enrich `content_document_id` carries only `069…` or null (the
  `801`/`a0A`/`a2P` overload is gone); typed links resolved
  (Order/Contract/Account + `Invoice__c` from SDoc `parent_type`).

**Still pending (needs a live org, per §7–§8):**

- A real `make archive` re-export to confirm row-count convergence (acceptance
  §6.1–§6.2): ContentVersion ~10k, ContentDocument ~8.6k, ContentDocumentLink
  in the thousands with `801`/`006`/`001`/`800` `LinkedEntityId` prefixes.
- The 10-active-Orders end-to-end signed-PDF spot check (§6.3 / §7).
- Confirm the export user has "View All Data" / adequate Files access so
  ContentDocumentLink visibility doesn't silently drop Order links (§8).
- Decide soft-deleted handling for the `archive-shutdown` path (§8).
