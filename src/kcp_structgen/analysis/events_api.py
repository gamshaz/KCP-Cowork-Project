"""Economic events loader: FMP primary, Bloomberg CSV fallback, cache.

Per spec §5.4:
  1. Per week, prefer a Bloomberg ECO CSV at data/events/bloomberg_eco_YYYY-Www.csv
     if present (manual desk fallback).
  2. Else fetch from FMP's free economic-calendar endpoint, cache as
     data/events/YYYY-Www.json.
  3. If both fail, return [] and let the caller flag in the rundown header.

This module is pure I/O + normalisation. Event matching (regex →
matcher key) and surprise classification (hot/cold/inline) live in
event_matcher.py and classifier.py respectively.
"""

from __future__ import annotations

import csv
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import SettingsError, get_fmp_api_key, get_fmp_base_url

HTTP_TIMEOUT_S = 30
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "events"


class EventsAPIError(RuntimeError):
    """Network failure, bad payload shape, or unparseable cache file."""


# ---------------------------------------------------------------------------
# Week math
# ---------------------------------------------------------------------------

def week_window(d: date) -> tuple[date, date]:
    """Return (Monday, Friday) of the ISO week containing `d`.

    Saturday/Sunday roll into the *previous* week's Friday-anchored window
    so a Sunday "generate rundown" picks up the week that just closed.
    """
    iso_year, iso_week, iso_weekday = d.isocalendar()
    # Monday of ISO week
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    friday = monday + timedelta(days=4)
    return monday, friday


def week_label(d: date) -> str:
    """Return ISO week label like '2026-W47' for the ISO week of `d`."""
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _normalise_fmp_event(raw: dict) -> dict | None:
    """Map one FMP calendar entry → our normalised event dict.

    Returns None if the row lacks the bare minimum (date or event name).
    """
    raw_date = raw.get("date")
    event_name = raw.get("event")
    if not raw_date or not event_name:
        return None
    # FMP date is "YYYY-MM-DD HH:MM:SS" UTC; we only need the date.
    iso_date = str(raw_date).split(" ", 1)[0]
    return {
        "date":       iso_date,
        "country":    raw.get("country") or None,
        "event_name": str(event_name),
        "matcher":    None,   # populated downstream by event_matcher
        "previous":   raw.get("previous"),
        "estimate":   raw.get("estimate"),
        "actual":     raw.get("actual"),
        "impact":     raw.get("impact"),
    }


def _normalise_bloomberg_csv_row(row: dict) -> dict | None:
    """Map one Bloomberg ECO CSV row → our normalised event dict.

    Bloomberg's export columns vary by terminal version but commonly include:
      Date Time | Event | Period | Survey | Actual | Prior | Revised | Country/Region

    We match on lower-case keys, accept the common variants, and emit the
    same shape as `_normalise_fmp_event`. Bad rows return None.
    """
    # Lowercase keys for tolerant matching.
    r = {str(k).strip().lower(): v for k, v in row.items()}

    raw_date = r.get("date time") or r.get("date") or r.get("release date")
    event_name = r.get("event") or r.get("indicator")
    if not raw_date or not event_name:
        return None

    # Strip time-of-day if present.
    iso_date = str(raw_date).split(" ", 1)[0]
    # Bloomberg date might be MM/DD/YYYY or DD/MM/YYYY — best effort.
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            iso_date = datetime.strptime(iso_date, fmt).date().isoformat()
            break
        except ValueError:
            continue

    def _maybe_float(v):
        if v is None or str(v).strip() == "":
            return None
        try:
            return float(str(v).replace(",", "").rstrip("%"))
        except ValueError:
            return None

    return {
        "date":       iso_date,
        "country":    r.get("country/region") or r.get("country") or "US",
        "event_name": str(event_name),
        "matcher":    None,
        "previous":   _maybe_float(r.get("prior") or r.get("previous")),
        "estimate":   _maybe_float(r.get("survey") or r.get("estimate") or r.get("forecast")),
        "actual":     _maybe_float(r.get("actual")),
        "impact":     r.get("relevance") or r.get("impact") or None,
    }


# ---------------------------------------------------------------------------
# Source: FMP HTTP
# ---------------------------------------------------------------------------

def _fetch_fmp_calendar(start: date, end: date) -> list[dict]:
    """Hit FMP's /stable/economic-calendar endpoint for the window.

    Returns the raw JSON list. Caller normalises.
    """
    api_key = get_fmp_api_key()
    base = get_fmp_base_url()
    qs = urllib.parse.urlencode({
        "from":   start.isoformat(),
        "to":     end.isoformat(),
        "apikey": api_key,
    })
    url = f"{base}/economic-calendar?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise EventsAPIError(f"FMP HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise EventsAPIError(f"FMP network error: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise EventsAPIError(
            f"FMP returned non-JSON payload (first 200 chars): {payload[:200]!r}"
        ) from exc
    if not isinstance(data, list):
        raise EventsAPIError(f"FMP returned unexpected shape: {type(data).__name__}")
    return data


# ---------------------------------------------------------------------------
# Source: Bloomberg CSV fallback
# ---------------------------------------------------------------------------

def _load_bloomberg_csv(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            ev = _normalise_bloomberg_csv_row(raw_row)
            if ev is not None:
                rows.append(ev)
    return rows


# ---------------------------------------------------------------------------
# Cache + filtering
# ---------------------------------------------------------------------------

def _cache_path(week_d: date, cache_dir: Path) -> Path:
    return cache_dir / f"{week_label(week_d)}.json"


def _bloomberg_csv_path(week_d: date, cache_dir: Path) -> Path:
    return cache_dir / f"bloomberg_eco_{week_label(week_d)}.csv"


def _filter_to_us_in_window(events: list[dict],
                            start: date, end: date) -> list[dict]:
    """Keep US-country events whose date falls within [start, end]."""
    s = start.isoformat()
    e = end.isoformat()
    kept = []
    for ev in events:
        if ev.get("country") and str(ev["country"]).upper() not in ("US", "UNITED STATES"):
            continue
        d = ev.get("date")
        if not d or not (s <= d <= e):
            continue
        kept.append(ev)
    return kept


def _write_cache(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(events, indent=2), encoding="utf-8")


def _read_cache(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EventsAPIError(f"cache file unreadable: {path}: {exc}") from exc
    if not isinstance(data, list):
        raise EventsAPIError(f"cache file has wrong shape: {path}")
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_events_for_week(week_d: date,
                          cache_dir: Path | None = None,
                          *,
                          use_cache: bool = True,
                          allow_network: bool = True) -> list[dict]:
    """Return US economic events for the ISO week containing `week_d`.

    Resolution order (matches spec §5.4):
      1. Bloomberg ECO CSV at <cache_dir>/bloomberg_eco_<week>.csv if present.
      2. JSON cache file at <cache_dir>/<week>.json if present and `use_cache`.
      3. Live FMP fetch (if `allow_network`), then write the cache.
      4. Empty list (caller flags "events unavailable").

    Network and settings failures raise `EventsAPIError`. The caller (runner)
    decides whether to suppress and degrade gracefully.
    """
    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    start, end = week_window(week_d)

    bb = _bloomberg_csv_path(week_d, cache_dir)
    if bb.is_file():
        events = _load_bloomberg_csv(bb)
        return _filter_to_us_in_window(events, start, end)

    cache = _cache_path(week_d, cache_dir)
    if use_cache and cache.is_file():
        events = _read_cache(cache)
        return _filter_to_us_in_window(events, start, end)

    if not allow_network:
        return []

    try:
        raw = _fetch_fmp_calendar(start, end)
    except SettingsError:
        # No key configured — return empty so the runner can flag.
        return []
    events = [e for e in (_normalise_fmp_event(r) for r in raw) if e is not None]
    if use_cache:
        _write_cache(cache, events)
    return _filter_to_us_in_window(events, start, end)
