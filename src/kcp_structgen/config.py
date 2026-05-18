"""Local settings — API keys, base URLs.

Reads `config/settings.json` at the repo root. Never committed (`.gitignore`).
A template lives at `config/settings.example.json` — desk members copy it,
paste their FMP key, save.

Kept tiny on purpose: Layer 2 has one external API key today (FMP). When more
arrive (Bloomberg, FRED, etc.) they go in the same file behind separate
getters.
"""

from __future__ import annotations

import json
from pathlib import Path

# settings.json sits at <repo>/config/settings.json — walk up from this file.
SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.json"
EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.example.json"


class SettingsError(RuntimeError):
    """Settings file missing, unreadable, or a required key not set."""


def _load_settings() -> dict:
    if not SETTINGS_PATH.is_file():
        raise SettingsError(
            f"settings file not found at {SETTINGS_PATH}.\n"
            f"Copy {EXAMPLE_PATH.name} → settings.json and paste your "
            "FMP API key into the new file."
        )
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SettingsError(f"settings.json is not valid JSON: {exc}") from exc


def get_fmp_api_key() -> str:
    """Return the FMP API key from `config/settings.json`."""
    s = _load_settings()
    key = s.get("fmp_api_key", "").strip()
    if not key or key.startswith("PASTE_"):
        raise SettingsError(
            "FMP API key not set in settings.json. "
            "Edit the file and paste your real key into 'fmp_api_key'."
        )
    return key


DEFAULT_FMP_BASE_URL = "https://financialmodelingprep.com/stable"


def get_fmp_base_url() -> str:
    """Base URL for FMP endpoints. Reads from settings.json if present;
    otherwise falls back to the stable v4 endpoint. The base URL is not a
    secret, so a missing settings file is fine here — only the API key
    requires explicit configuration."""
    try:
        s = _load_settings()
    except SettingsError:
        return DEFAULT_FMP_BASE_URL
    return s.get("fmp_base_url", DEFAULT_FMP_BASE_URL).rstrip("/")
