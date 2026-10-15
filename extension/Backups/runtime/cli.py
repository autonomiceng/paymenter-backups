#!/usr/bin/env python3
"""Isolated bundled entry point; invoke with Python -I."""
import pathlib
import sys
sys.dont_write_bytecode = True

runtime = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(runtime / "sdk"))
sys.path.insert(0, str(runtime / "src"))
from paymenter_backups.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
