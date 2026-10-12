"""Local, read-only MCP server for my PushPress member data."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("PUSHPRESS_MCP_STATE_DIR", PROJECT_ROOT / "state"))
SESSION_PATH = STATE_DIR / "session.json"
DB_PATH = STATE_DIR / "pushpress.db"
