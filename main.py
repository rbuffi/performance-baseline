#!/usr/bin/env python3
"""Entry point for environments that can only invoke main.py."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from perfbaseline.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
