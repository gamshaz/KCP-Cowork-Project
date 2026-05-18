"""Tests for events_api.

Network calls are mocked. No real HTTP traffic from tests. The FMP shape
mirrored here is from Opus's research note and the Apr 2026 working sample.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest

from kcp_structgen.analysis.events_api import (
    EventsAPIError,
    _filter_to_us_in_window,
    _normalise_bloomberg_csv_row,
    _normalise_fmp_event,
    load_events_for_week,
    week_label,
    week_window,
)


# ---------------------------------------------------------------------------
# Week math
# ---------------------------------------------------------------------------

def test_week_window_wednesday():
    """Wednesday 18 Nov 2026 → Mon 16 / Fri 20."""
    mon, fri = week_window(date(2026, 11, 18))
    assert mon == date(2026, 11, 16)
    assert fri == date(2026, 11, 20)


def test_week_window_sunday_rolls_into_same_iso_week():
    """ISO weeks run Mon→Sun. Sunday is the LAST day of its ISO week, so a
    Sunday `week_window` gives that same Monday-Friday — the week that just
    closed."""
    mon, fri = week_window(date(2026, 11, 22))  # Sunday
    assert mon == date(2026, 11, 16)
    assert fri == date(2026, 11, 20)


def test_week_label_format():
    assert week_label(date(2026, 11, 18)) == "2026-W47"
    assert week_label(date(2026, 1, 5)) == "2026-W02"


# ---------------------------------------------------------------------------
# FMP normalisation
# ---------------------------------------------------------------------------

def test_normalise_fmp_event_full():
    raw = {
        "date":      "2026-11-18 13:30:00",
        "country":   "US",
        "event":     "Consumer Price Index YoY",
        "previous":  2.7,
        "estimate":  2.9,
        "actual":    3.2,
        "change":    0.5,
        "impact":    "High",
    }
    ev = _normalise_fmp_event(raw)
    assert ev["date"] == "2026-11-18"
    assert ev["country"] == "US"
    assert ev["event_name"] == "Consumer Price Index YoY"
    assert ev["previous"] == 2.7
    assert ev["estimate"] == 2.9
    assert ev["actual"] == 3.2
    assert ev["impact"] == "High"
    assert ev["matcher"] is None


def test_normalise_fmp_event_missing_required():
    assert _normalise_fmp_event({"event": "no date"}) is None
    assert _normalise_fmp_event({"date": "2026-11-18", "event": ""}) is None


def test_normalise_fmp_event_no_actual_for_future():
    """Future events have null actual — must pass through, not crash."""
    raw = {
        "date":     "2027-01-15 13:30:00",
        "country":  "US",
        "event":    "Non Farm Payrolls",
        "previous": 200,
        "estimate": 175,
        "actual":   None,
        "impact":   "High",
    }
    ev = _normalise_fmp_event(raw)
    assert ev["actual"] is None


# ---------------------------------------------------------------------------
# Bloomberg CSV normalisation
# ---------------------------------------------------------------------------

def test_normalise_bloomberg_row_full():
    raw = {
        "Date Time":      "11/18/2026 08:30",
        "Event":          "CPI YoY",
        "Period":         "Oct",
        "Survey":         "2.9",
        "Actual":         "3.2",
        "Prior":          "2.7",
        "Country/Region": "US",
        "Relevance":      "High",
    }
    ev = _normalise_bloomberg_csv_row(raw)
    assert ev["date"] == "2026-11-18"
    assert ev["country"] == "US"
    assert ev["event_name"] == "CPI YoY"
    assert ev["previous"] == 2.7
    assert ev["estimate"] == 2.9
    assert ev["actual"] == 3.2
    assert ev["impact"] == "High"


def test_normalise_bloomberg_row_missing_fields():
    assert _normalise_bloomberg_csv_row({}) is None
    assert _normalise_bloomberg_csv_row({"Event": "x", "Date Time": ""}) is None


def test_normalise_bloomberg_row_percent_in_value():
    raw = {
        "Date": "2026-11-18",
        "Event": "CPI YoY",
        "Survey": "2.9%",
        "Actual": "3.2%",
    }
    ev = _normalise_bloomberg_csv_row(raw)
    assert ev["estimate"] == 2.9
    assert ev["actual"] == 3.2


# ---------------------------------------------------------------------------
# US + window filter
# ---------------------------------------------------------------------------

def test_filter_to_us_in_window_keeps_us():
    events = [
        {"date": "2026-11-18", "country": "US", "event_name": "CPI"},
        {"date": "2026-11-18", "country": "EU", "event_name": "ECB"},
        {"date": "2026-11-18", "country": "United States", "event_name": "Other"},
    ]
    kept = _filter_to_us_in_window(events, date(2026, 11, 16), date(2026, 11, 20))
    assert len(kept) == 2
    assert all(e["country"] in ("US", "United States") for e in kept)


def test_filter_to_us_in_window_excludes_outside():
    events = [
        {"date": "2026-11-15", "country": "US", "event_name": "before"},
        {"date": "2026-11-18", "country": "US", "event_name": "in"},
        {"date": "2026-11-21", "country": "US", "event_name": "after"},
    ]
    kept = _filter_to_us_in_window(events, date(2026, 11, 16), date(2026, 11, 20))
    assert [e["event_name"] for e in kept] == ["in"]


# ---------------------------------------------------------------------------
# load_events_for_week — integration with mocked HTTP
# ---------------------------------------------------------------------------

SAMPLE_FMP_PAYLOAD = [
    {
        "date":     "2026-11-18 13:30:00",
        "country":  "US",
        "event":    "Consumer Price Index YoY",
        "previous": 2.7,
        "estimate": 2.9,
        "actual":   3.2,
        "impact":   "High",
    },
    {
        "date":     "2026-11-20 13:30:00",
        "country":  "US",
        "event":    "Initial Jobless Claims",
        "previous": 210000,
        "estimate": 215000,
        "actual":   208000,
        "impact":   "Medium",
    },
    {
        "date":     "2026-11-18 10:00:00",
        "country":  "EU",
        "event":    "ECB Speech",
        "impact":   "Low",
    },
]


class _FakeResponse:
    def __init__(self, payload: str):
        self._payload = payload.encode("utf-8")
    def read(self):
        return self._payload
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_load_events_for_week_fmp_path_writes_cache(tmp_path):
    """Live FMP fetch path: hits mocked HTTP, writes cache, filters US."""
    body = json.dumps(SAMPLE_FMP_PAYLOAD)

    with patch("kcp_structgen.analysis.events_api.get_fmp_api_key", return_value="FAKEKEY"), \
         patch("urllib.request.urlopen",
               return_value=_FakeResponse(body)):
        events = load_events_for_week(date(2026, 11, 18), cache_dir=tmp_path)

    # Filtered to US only
    assert len(events) == 2
    names = {e["event_name"] for e in events}
    assert "Consumer Price Index YoY" in names
    assert "Initial Jobless Claims" in names
    assert "ECB Speech" not in names

    # Cache written for the week
    cache = tmp_path / "2026-W47.json"
    assert cache.is_file()
    cached = json.loads(cache.read_text())
    assert len(cached) == 3  # Pre-filter (all countries) is cached; filter on read


def test_load_events_for_week_cache_hit_no_network(tmp_path):
    """If the cache exists, no HTTP call is made."""
    cache = tmp_path / "2026-W47.json"
    cache.write_text(json.dumps(SAMPLE_FMP_PAYLOAD).replace(
        '"event":', '"event_name":'   # cache stores normalised shape
    ))
    # Hand-craft normalised cache file:
    normalised = [
        {"date": "2026-11-18", "country": "US",
         "event_name": "Consumer Price Index YoY", "matcher": None,
         "previous": 2.7, "estimate": 2.9, "actual": 3.2, "impact": "High"},
    ]
    cache.write_text(json.dumps(normalised))

    with patch("urllib.request.urlopen") as mock_urlopen:
        events = load_events_for_week(date(2026, 11, 18), cache_dir=tmp_path)
        mock_urlopen.assert_not_called()

    assert len(events) == 1
    assert events[0]["event_name"] == "Consumer Price Index YoY"


def test_load_events_for_week_bloomberg_csv_overrides_cache(tmp_path):
    """If a Bloomberg CSV is dropped for the week, it wins over the JSON cache."""
    # Drop a Bloomberg CSV
    bb = tmp_path / "bloomberg_eco_2026-W47.csv"
    bb.write_text(
        "Date Time,Event,Survey,Actual,Prior,Country/Region\n"
        "2026-11-18 08:30,CPI YoY,2.9,3.2,2.7,US\n",
        encoding="utf-8",
    )
    # Also drop a JSON cache that would say something different
    cache = tmp_path / "2026-W47.json"
    cache.write_text(json.dumps([{
        "date": "2026-11-18", "country": "US",
        "event_name": "STALE CACHE", "matcher": None,
        "previous": 0, "estimate": 0, "actual": 0, "impact": None,
    }]))

    events = load_events_for_week(date(2026, 11, 18), cache_dir=tmp_path)
    assert len(events) == 1
    assert events[0]["event_name"] == "CPI YoY"  # Bloomberg wins
    assert events[0]["actual"] == 3.2


def test_load_events_for_week_no_network_returns_empty(tmp_path):
    """allow_network=False with no cache and no Bloomberg CSV → []."""
    events = load_events_for_week(date(2026, 11, 18),
                                  cache_dir=tmp_path,
                                  allow_network=False)
    assert events == []


def test_load_events_for_week_settings_error_returns_empty(tmp_path):
    """SettingsError (no API key) returns [] gracefully so runner can flag."""
    from kcp_structgen.config import SettingsError

    def _raise(*a, **kw):
        raise SettingsError("no key")

    with patch("kcp_structgen.analysis.events_api.get_fmp_api_key", side_effect=_raise):
        events = load_events_for_week(date(2026, 11, 18), cache_dir=tmp_path)
    assert events == []


def test_load_events_for_week_http_error_raises(tmp_path):
    import urllib.error

    def _raise_http(*a, **kw):
        raise urllib.error.HTTPError("url", 500, "Server Error", {}, None)

    with patch("kcp_structgen.analysis.events_api.get_fmp_api_key", return_value="FAKEKEY"), \
         patch("urllib.request.urlopen",
               side_effect=_raise_http):
        with pytest.raises(EventsAPIError, match="HTTP 500"):
            load_events_for_week(date(2026, 11, 18), cache_dir=tmp_path)


def test_load_events_for_week_bad_json_raises(tmp_path):
    with patch("kcp_structgen.analysis.events_api.get_fmp_api_key", return_value="FAKEKEY"), \
         patch("urllib.request.urlopen",
               return_value=_FakeResponse("<html>error page</html>")):
        with pytest.raises(EventsAPIError, match="non-JSON"):
            load_events_for_week(date(2026, 11, 18), cache_dir=tmp_path)


def test_load_events_for_week_unexpected_shape_raises(tmp_path):
    with patch("kcp_structgen.analysis.events_api.get_fmp_api_key", return_value="FAKEKEY"), \
         patch("urllib.request.urlopen",
               return_value=_FakeResponse('{"error": "wrong shape"}')):
        with pytest.raises(EventsAPIError, match="unexpected shape"):
            load_events_for_week(date(2026, 11, 18), cache_dir=tmp_path)
