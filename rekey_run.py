#!/usr/bin/env python3
"""Launcher so the tool runs under ``python -I`` (isolated mode).

Usage:
    python -I rekey_run.py check   input.db
    python -I rekey_run.py preview input.db
    python -I rekey_run.py build   input.db output.db
    python -I rekey_run.py explain output.db --customer P --date ... --metric m
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rekey.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
