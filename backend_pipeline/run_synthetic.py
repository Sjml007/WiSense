#!/usr/bin/env python3
"""Convenience runner for the WiSense-Twin Synthetic CSI Data Generator."""

import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wisense_backend.ingestion.synthetic_source import run_cli

if __name__ == "__main__":
    run_cli()
