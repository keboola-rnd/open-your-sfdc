#!/usr/bin/env python3
"""
Salesforce connection helper.

Loads credentials from the repo-local ``.env`` file (or process environment)
and returns an authenticated ``simple_salesforce.Salesforce`` instance.

Also exposes ``flatten_record()`` which is used by the export scripts to turn
nested lookup results (e.g. ``Account.Name``) into flat CSV columns.

Required variables:
    SF_USERNAME=your.email@example.com
    SF_PASSWORD=your_password
    SF_SECURITY_TOKEN=your_token
    SF_DOMAIN=login              # or test / <mydomain>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from simple_salesforce import Salesforce
except ImportError:
    print("Error: simple-salesforce is not installed.")
    print("Run: pip install simple-salesforce")
    sys.exit(1)


# scripts/ lives at <repo-root>/scripts -> parent.parent is the repo root.
ROOT_DIR = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Populate os.environ from the repo-root ``.env`` file if present.

    Only sets variables that aren't already defined so explicit exports win.
    Supports inline comments (``KEY=value # comment``) for unquoted values.
    """
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Strip inline ``# comment`` when the value isn't a quoted string.
            if value and value[0] not in ("'", '"'):
                hash_pos = value.find(" #")
                if hash_pos >= 0:
                    value = value[:hash_pos].rstrip()
                # Safety net: trim stray whitespace that snuck in via copy-paste.
                value = value.strip()
            # Strip surrounding quotes from value (handle both "..." and '...').
            if value and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def get_connection() -> Salesforce:
    """Return an authenticated Salesforce instance.

    Credentials come from environment variables (loaded from ``.env`` if
    available). The script exits with a clear message when any required
    variable is missing.
    """
    load_env()

    username = os.getenv("SF_USERNAME")
    password = os.getenv("SF_PASSWORD")
    token = os.getenv("SF_SECURITY_TOKEN")
    domain = os.getenv("SF_DOMAIN", "login")

    if not all([username, password, token]):
        print("Error: Missing Salesforce credentials in environment / .env")
        print("Required variables: SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN")
        sys.exit(1)

    return Salesforce(
        username=username,
        password=password,
        security_token=token,
        domain=domain,
    )


def flatten_record(record: dict, prefix: str = "") -> dict:
    """Flatten nested SObject dicts into single-level key/value pairs.

    A nested relationship field like ``Account`` becomes multiple columns
    prefixed with ``Account.`` (e.g. ``Account.Name``). The SF metadata key
    ``attributes`` is always discarded.
    """
    flat: dict = {}
    for key, value in record.items():
        if key == "attributes":
            continue
        full_key = f"{prefix}{key}" if prefix else key
        if value is None:
            flat[full_key] = None
        elif isinstance(value, dict):
            flat.update(flatten_record(value, f"{full_key}."))
        else:
            flat[full_key] = value
    return flat
