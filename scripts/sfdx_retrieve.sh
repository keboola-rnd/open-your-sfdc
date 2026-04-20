#!/usr/bin/env bash
#
# Retrieve full XML metadata from a Salesforce org via the SFDX CLI.
#
# Covers the types that are NOT queryable via REST / Tooling SOQL:
# Layouts, Profiles, PermissionSets, CustomLabels, FlexiPages, ConnectedApps,
# NamedCredentials, full CustomObject XML, CustomMetadataType definitions,
# ApprovalProcesses, SharingRules, Translations, Settings, Certificates
# (public parts only), DigitalExperienceBundle, etc.
#
# Usage:
#   scripts/sfdx_retrieve.sh                  # Uses $SFDX_ORG_ALIAS from .env
#   scripts/sfdx_retrieve.sh my-alias         # Override target org alias
#
# Prerequisites (one-time setup):
#   1. Install the CLI:   npm install -g @salesforce/cli
#                         # or: brew install salesforcecli/taps/sf
#   2. Authenticate:      sf org login web --alias <alias>
#                         # opens a browser; log in to the org you want to backup
#   3. Remember the alias and put it in .env:
#                         SFDX_ORG_ALIAS=<alias>
#
# Output:
#   data/sf_sfdx_metadata/main/default/
#       layouts/*.layout-meta.xml
#       flows/*.flow-meta.xml
#       profiles/*.profile-meta.xml
#       ... one subdirectory per metadata type, with one file per component
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$ROOT_DIR/config/sfdx_package.xml"
OUTPUT_DIR="$ROOT_DIR/data/sf_sfdx_metadata"
PROJECT_FILE="$ROOT_DIR/sfdx-project.json"
PROJECT_TEMPLATE="$ROOT_DIR/config/sfdx-project.json"

# ------------------------------------------------------------------
# 1. Sanity checks
# ------------------------------------------------------------------

if ! command -v sf >/dev/null 2>&1; then
    cat >&2 <<EOF
ERROR: The Salesforce CLI ('sf') is not installed.

Install (pick one, in order of Salesforce's current recommendation):

  1. Official .pkg installer (macOS, recommended):
     https://developer.salesforce.com/tools/salesforcecli
     → download sf-arm64.pkg (Apple Silicon) or sf-x64.pkg (Intel)

  2. npm (requires Node.js LTS):
     npm install @salesforce/cli --global

NOTE: the old 'brew install salesforcecli/taps/sf' tap has been removed;
the 'brew install --cask salesforce-cli' is deprecated (disabled 2026-09-01).

Then authenticate (one-time):
    sf org login web --alias my-org
EOF
    exit 127
fi

if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: Missing manifest at $MANIFEST" >&2
    exit 1
fi

# Load .env for SFDX_ORG_ALIAS if the user set one.
if [ -f "$ROOT_DIR/.env" ]; then
    # Pull only the one variable we care about; never echo secrets.
    set +u
    SFDX_ORG_ALIAS_ENV="$(grep -E '^SFDX_ORG_ALIAS=' "$ROOT_DIR/.env" | tail -n1 | cut -d'=' -f2- | tr -d '"'"'"'' )"
    set -u
fi

ORG_ALIAS="${1:-${SFDX_ORG_ALIAS:-${SFDX_ORG_ALIAS_ENV:-}}}"
if [ -z "$ORG_ALIAS" ]; then
    cat >&2 <<EOF
ERROR: No org alias provided.

Set one of:
  - SFDX_ORG_ALIAS in .env
  - SFDX_ORG_ALIAS environment variable
  - pass as first arg: scripts/sfdx_retrieve.sh my-alias

You authenticated with: sf org login web --alias <alias>
EOF
    exit 1
fi

# ------------------------------------------------------------------
# 2. sfdx-project.json: SFDX requires it in the CWD of the retrieve.
#    We keep the authoritative copy under config/ and symlink it at
#    the repo root only while the retrieve is running.
# ------------------------------------------------------------------

cleanup_project_file=0
if [ ! -f "$PROJECT_FILE" ]; then
    cp "$PROJECT_TEMPLATE" "$PROJECT_FILE"
    cleanup_project_file=1
fi
trap '[ "$cleanup_project_file" = "1" ] && rm -f "$PROJECT_FILE"' EXIT

mkdir -p "$OUTPUT_DIR"

# ------------------------------------------------------------------
# 3. Retrieve
# ------------------------------------------------------------------

echo "=============================================================="
echo "SFDX metadata retrieve"
echo "=============================================================="
echo "Org alias:     $ORG_ALIAS"
echo "Manifest:      $MANIFEST"
echo "Output:        $OUTPUT_DIR"
echo

cd "$ROOT_DIR"

# No --output-dir: SFDX refuses if it overlaps a packageDirectory, which ours
# intentionally does (we point the default package at data/sf_sfdx_metadata).
# Without --output-dir SFDX writes to packageDirectories[0].path, which is
# exactly where we want the files to end up.
#
# --ignore-conflicts lets us overwrite a prior retrieve cleanly.
# --wait 60 gives large orgs time to finish (default 33 min, enough for most).
sf project retrieve start \
    --target-org "$ORG_ALIAS" \
    --manifest "$MANIFEST" \
    --ignore-conflicts \
    --wait 60

echo
echo "=============================================================="
echo "Done. Files under $OUTPUT_DIR"
echo "=============================================================="
