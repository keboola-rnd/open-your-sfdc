.PHONY: help install export export-dry import refresh files files-dry files-audit files-gaps describe browser browser-stop

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

export: ## Export ALL Salesforce objects to CSV (data/sf_full_export/)
	.venv/bin/python scripts/export_all.py

export-dry: ## Dry-run: list what would be exported
	.venv/bin/python scripts/export_all.py --dry-run

import: ## Import CSV export into SQLite (data/salesforce_full.db)
	.venv/bin/python scripts/import_to_sqlite.py

refresh: export import ## Full: export + import into SQLite

describe: ## Fetch object descriptions from SF and add to _sf_objects table
	.venv/bin/python scripts/fetch_descriptions.py

##@ Download binary files (Attachments, ContentVersion, SDocs PDFs)

files: ## Download all binaries referenced by ContentVersion / Attachment / Document
	.venv/bin/python scripts/download_files.py

files-dry: ## Dry-run: list files that would be downloaded
	.venv/bin/python scripts/download_files.py --dry-run

files-audit: ## Audit: compare DB vs disk vs external refs (no downloads)
	-.venv/bin/python scripts/audit_files.py

files-gaps: ## Download files referenced from FeedAttachment / ContentAsset / SDocs that were skipped
	.venv/bin/python scripts/download_files.py --type external --resume

##@ Browse your exported data

browser: ## Start the Salesforce Data Browser (port 5003)
	.venv/bin/python run.py

browser-stop: ## Stop the Salesforce Data Browser
	@lsof -ti :5003 | xargs kill 2>/dev/null && echo "Browser stopped." || echo "Browser not running."
