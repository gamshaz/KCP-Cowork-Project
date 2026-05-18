"""Load the street flow Excel and the KCP client-trades Excel.

Two separate streams (per spec §5.2 + §5.3) sharing one schema. Same row
shape, separate sheets, separate calls. The aggregator keeps them apart so
the rundown can talk about street activity vs the desk's own client book
independently.

No LLM call. Tolerant: rows with missing structured columns still emit with
those fields as None — the `raw_note` is always preserved. The LLM reads
raw_note when structured fields are sparse.
"""

from __future__ import annotations

import re
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

REQUIRED_COLUMNS = ("date", "raw_note")
OPTIONAL_COLUMNS = ("product", "expiry", "structure", "size", "direction", "price")
ALL_KNOWN_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

VALID_PRODUCTS = frozenset({"SR3", "0Q"})
EXPIRY_RE = re.compile(r"^[FGHJKMNQUVXZ]\d$")

# Desk-speak normalisation. Order matters — first match wins.
# "on the bid" / "on bid" → sell (someone bought on the seller's bid)
# bare "bid" → buy (as in "paper bid")
BUY_ALIASES = frozenset({
    "buy", "bought", "buyer", "paid", "lifted", "lift",
    "taken", "took", "bid",
})
SELL_ALIASES = frozenset({
    "sell", "sold", "seller", "hit", "offered", "offer", "given", "gave",
    "on bid", "on the bid",
})


class FlowLoaderError(ValueError):
    """File missing, sheet unreadable, required column absent."""


# ---------------------------------------------------------------------------
# Field normalisers
# ---------------------------------------------------------------------------

def _normalise_date(raw: Any) -> str | None:
    """Convert a cell value to ISO date string, or None if unparseable.

    Accepts: datetime, date, pandas Timestamp, "YYYY-MM-DD", "DD/MM/YYYY",
    "MM/DD/YYYY" (best-effort), Excel serial numbers (via pandas).
    """
    if raw is None:
        return None
    if isinstance(raw, float) and pd.isna(raw):
        return None
    if isinstance(raw, (datetime, pd.Timestamp)):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return None
    # Try common formats; let pandas handle the rest.
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return pd.to_datetime(s).date().isoformat()
    except (ValueError, TypeError):
        return None


def _normalise_raw_note(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, float) and pd.isna(raw):
        return None
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def _normalise_product(raw: Any) -> str | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    if s in VALID_PRODUCTS:
        return s
    warnings.warn(f"unknown product {raw!r}; emitting as None")
    return None


def _normalise_expiry(raw: Any) -> str | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    if EXPIRY_RE.match(s):
        return s
    warnings.warn(f"bad expiry {raw!r}; emitting as None")
    return None


def _normalise_direction(raw: Any) -> str | None:
    """Map trader-speak to {'buy', 'sell'}.

    Two-word phrases ('on bid', 'on the bid') are checked first because
    they negate the single-word default.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    # Multi-word negations first (so 'on bid' doesn't fall through to 'bid' → buy)
    if s in SELL_ALIASES:
        return "sell"
    if s in BUY_ALIASES:
        return "buy"
    warnings.warn(f"unknown direction {raw!r}; emitting as None")
    return None


def _normalise_size(raw: Any) -> int | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, (int,)):
        return int(raw) if raw != 0 else None
    if isinstance(raw, float):
        v = int(raw)
        return v if v != 0 else None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    # Accept '5k' shorthand
    if s.lower().endswith("k"):
        try:
            return int(float(s[:-1]) * 1000)
        except ValueError:
            warnings.warn(f"bad size {raw!r}; emitting as None")
            return None
    try:
        return int(float(s))
    except ValueError:
        warnings.warn(f"bad size {raw!r}; emitting as None")
        return None


def _normalise_price(raw: Any) -> float | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        warnings.warn(f"bad price {raw!r}; emitting as None")
        return None


def _normalise_structure(raw: Any) -> str | None:
    """Accept verbatim; no validation in this loader."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip()
    return s or None


# ---------------------------------------------------------------------------
# Loader core
# ---------------------------------------------------------------------------

def _load_rows(xlsx_path: Path | str) -> list[dict]:
    """Shared loader for street flow and client trades.

    Schema is identical between the two files (per spec §5.2 + §5.3); only
    the *meaning* differs (street intel vs KCP trades), so the loader is one
    function and the public functions are thin wrappers.
    """
    path = Path(xlsx_path)
    if not path.is_file():
        raise FlowLoaderError(f"file not found: {path}")

    try:
        df = pd.read_excel(path)
    except Exception as exc:
        raise FlowLoaderError(
            f"failed to read {path.name}: {type(exc).__name__}: {exc}"
        ) from exc

    # Header normalisation: trim whitespace, lower-case for matching.
    df.columns = [str(c).strip().lower() for c in df.columns]

    for required in REQUIRED_COLUMNS:
        if required not in df.columns:
            raise FlowLoaderError(
                f"required column {required!r} missing from {path.name}; "
                f"found: {list(df.columns)}"
            )

    if df.empty:
        warnings.warn(f"{path.name} has no data rows")
        return []

    rows: list[dict] = []
    for idx, row in df.iterrows():
        d = _normalise_date(row.get("date"))
        if d is None:
            warnings.warn(f"row {idx} in {path.name}: missing/bad date, skipped")
            continue
        note = _normalise_raw_note(row.get("raw_note"))
        if note is None:
            warnings.warn(f"row {idx} in {path.name}: missing raw_note, skipped")
            continue
        rows.append({
            "date":      d,
            "raw_note":  note,
            "product":   _normalise_product(row.get("product")),
            "expiry":    _normalise_expiry(row.get("expiry")),
            "structure": _normalise_structure(row.get("structure")),
            "size":      _normalise_size(row.get("size")),
            "direction": _normalise_direction(row.get("direction")),
            "price":     _normalise_price(row.get("price")),
        })
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_flow(xlsx_path: Path | str) -> list[dict]:
    """Load the street flow Excel.

    Returns a list of normalised rows. Bad rows are skipped with warnings.
    Required columns: `date`, `raw_note`. All others are optional.
    """
    return _load_rows(xlsx_path)


def load_client_trades(xlsx_path: Path | str) -> list[dict]:
    """Load the KCP client-trades Excel. Same shape as `load_flow`."""
    return _load_rows(xlsx_path)


def filter_rows_to_window(rows: list[dict], start: date, end: date) -> list[dict]:
    """Return rows whose `date` falls in [start, end] inclusive.

    Used by aggregator/runner to scope rows to a week or segment.
    """
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    return [r for r in rows if start_iso <= r["date"] <= end_iso]
