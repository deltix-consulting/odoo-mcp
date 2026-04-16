"""Shared test fixtures.

We mutate ``sys.path`` so the tests can import ``odoo_mcp`` without an install
step — this way ``uv run pytest`` Just Works from a fresh checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
