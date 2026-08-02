from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "collection_tracker.sqlite3"
TOKEN_CACHE_PATH = DATA_DIR / "echomtg_token.json"
CONFIG_JSON_PATH = BASE_DIR / "config.json"

DEFAULT_SLEEVE_THRESHOLD = 1.00
DEFAULT_TOPLOAD_THRESHOLD = 10.00


def _load_config_json() -> dict:
    if not CONFIG_JSON_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_JSON_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


_CONFIG_JSON = _load_config_json()


def _resolve(key: str, default: Optional[Any] = None) -> Optional[Any]:
    """Resolution order: env var -> config.json -> default."""
    if key in os.environ and os.environ[key] != "":
        return os.environ[key]
    if key in _CONFIG_JSON and _CONFIG_JSON[key] != "":
        return _CONFIG_JSON[key]
    return default


def get_permanent_api_key() -> Optional[str]:
    value = _resolve("ECHOMTG_API_KEY")
    return value or None


def get_default_sleeve_threshold() -> float:
    return float(_resolve("SLEEVE_THRESHOLD", DEFAULT_SLEEVE_THRESHOLD))


def get_default_topload_threshold() -> float:
    return float(_resolve("TOPLOAD_THRESHOLD", DEFAULT_TOPLOAD_THRESHOLD))


def get_sync_interval_hours() -> float:
    return float(_resolve("SYNC_INTERVAL_HOURS", 24))
