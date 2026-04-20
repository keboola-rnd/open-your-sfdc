#!/usr/bin/env python3
"""Run the Salesforce Data Browser Flask application."""

import sys
from pathlib import Path

# Make the ``sf_browser`` package importable when this file is executed
# directly (e.g. ``python run.py``) from anywhere.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from sf_browser.app import create_app

app = create_app()

if __name__ == "__main__":
    print("Salesforce Data Browser running at http://localhost:5003")
    app.run(debug=True, port=5003)
