.PHONY: help install export export-dry export-history import refresh files files-force files-dry files-audit files-gaps describe metadata metadata-dry sfdx-check sfdx-retrieve event-logs event-logs-dry archive archive-audit browser browser-stop

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} \
	     /^##@ / {printf "\n\033[1;33m%s\033[0m\n", substr($$0, 5); next} \
	     /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' \
	     $(MAKEFILE_LIST)

##@ Setup

install: ## Install Python dependencies into .venv
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

##@ Export data from Salesforce

export: ## Export ALL Salesforce objects to CSV (full refresh; overwrites data/sf_full_export/)
	.venv/bin/python scripts/export_all.py

export-dry: ## Dry-run: list what would be exported
	.venv/bin/python scripts/export_all.py --dry-run

export-history: ## Export including *__History field-history tables (full refresh; can be huge)
	.venv/bin/python scripts/export_all.py --include-history

import: ## Drop + rebuild SQLite DB from CSV (data/salesforce_full.db)
	.venv/bin/python scripts/import_to_sqlite.py

refresh: export import ## Full refresh: export + import (~50 min for a mid-size org)

describe: ## Fetch object descriptions from SF and add to _sf_objects table
	.venv/bin/python scripts/fetch_descriptions.py

##@ Download binary files (Attachments, ContentVersion, SDocs PDFs)

files: ## Download all binaries (ContentVersion / Attachment / Document / SDocs / Chatter) — skips already-downloaded
	.venv/bin/python scripts/download_files.py --resume

files-force: ## Re-download everything including files already on disk
	.venv/bin/python scripts/download_files.py

files-dry: ## Dry-run: list files that would be downloaded
	.venv/bin/python scripts/download_files.py --dry-run

files-audit: ## Audit: compare DB vs disk vs external refs (no downloads)
	-.venv/bin/python scripts/audit_files.py

files-gaps: ## Download ONLY external refs (SDocs PDFs / Chatter / ContentAsset)
	.venv/bin/python scripts/download_files.py --type external --resume

##@ Metadata via Tooling API (Flow, Workflow, Validation, LWC, packages)

metadata: ## Tooling API: Flow / ValidationRule / Workflow / LWC / CustomField / CronTrigger / InstalledPackage (resume: skip existing JSON)
	.venv/bin/python scripts/export_metadata.py --resume

metadata-dry: ## Dry-run metadata export (counts only, no file writes)
	.venv/bin/python scripts/export_metadata.py --dry-run

##@ SFDX metadata retrieve (Layouts, Profiles, CustomLabels, full XML)

sfdx-check: ## Verify the Salesforce CLI is installed and authenticated
	@command -v sf >/dev/null || { \
	  echo "ERROR: sf CLI not installed."; \
	  echo "  1) Recommended: download .pkg from https://developer.salesforce.com/tools/salesforcecli"; \
	  echo "  2) Alt: npm install @salesforce/cli --global"; \
	  exit 127; }
	@sf --version
	@sf org list auths 2>/dev/null || echo "(no orgs authenticated yet — run: sf org login web --alias <alias>)"

sfdx-retrieve: ## Retrieve Layouts/Profiles/CustomLabels etc. as XML (full overwrite; needs sf CLI + SFDX_ORG_ALIAS)
	scripts/sfdx_retrieve.sh

##@ Event Monitoring logs (1-day retention without licence!)

event-logs: ## Download EventLogFile binaries (last 365 days, skip already downloaded)
	.venv/bin/python scripts/download_event_logs.py --resume

event-logs-dry: ## Dry-run: show what EventLogFile rows would be downloaded
	.venv/bin/python scripts/download_event_logs.py --dry-run

##@ One-shot full archive (export + files + metadata + sfdx + event-logs + audit)

archive: ## Full backup: data + binaries + metadata + SFDX + event logs + audit
	@echo "== 1/6 Export data =="
	$(MAKE) export
	@echo "\n== 2/6 Import to SQLite =="
	$(MAKE) import
	@echo "\n== 3/6 Download binaries =="
	$(MAKE) files
	@echo "\n== 4/6 Fetch SDocs + chatter refs =="
	$(MAKE) files-gaps
	@echo "\n== 5/6 Tooling API metadata =="
	$(MAKE) metadata
	@echo "\n== 6/6 SFDX retrieve (may skip if CLI missing) =="
	-$(MAKE) sfdx-retrieve
	@echo "\n== Optional: Event Monitoring logs =="
	-$(MAKE) event-logs
	@echo "\n== Final: archive audit =="
	-$(MAKE) archive-audit

archive-audit: ## Audit: what ran, what's missing, what to do next
	-.venv/bin/python scripts/audit_archive.py

##@ Browse your exported data

browser: ## Start the Salesforce Data Browser (port 5003)
	.venv/bin/python run.py

browser-stop: ## Stop the Salesforce Data Browser
	@lsof -ti :5003 | xargs kill 2>/dev/null && echo "Browser stopped." || echo "Browser not running."
