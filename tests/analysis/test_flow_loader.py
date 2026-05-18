"""Tests for flow_loader.

Fixtures are generated programmatically via openpyxl into tmp_path.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import openpyxl
import pytest

from kcp_structgen.analysis.flow_loader import (
    FlowLoaderError,
    _normalise_date,
    _normalise_direction,
    _normalise_expiry,
    _normalise_price,
    _normalise_product,
    _normalise_size,
    filter_rows_to_window,
    load_client_trades,
    load_flow,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_xlsx(path: Path, headers: list[str], rows: list[list]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)


HEADERS_FULL = ["date", "raw_note", "product", "expiry", "structure",
                "size", "direction", "price"]


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------

def test_normalise_date_iso():
    assert _normalise_date("2026-11-17") == "2026-11-17"


def test_normalise_date_european_format():
    assert _normalise_date("17/11/2026") == "2026-11-17"


def test_normalise_date_python_date():
    assert _normalise_date(date(2026, 11, 17)) == "2026-11-17"


def test_normalise_date_garbage():
    assert _normalise_date("not a date") is None
    assert _normalise_date(None) is None
    assert _normalise_date("") is None


def test_normalise_direction_buy_aliases():
    for alias in ("buy", "bought", "BOUGHT", "Paid", "lifted", "taken", "bid"):
        assert _normalise_direction(alias) == "buy", f"failed for {alias!r}"


def test_normalise_direction_sell_aliases():
    for alias in ("sell", "sold", "SOLD", "Hit", "offered", "given"):
        assert _normalise_direction(alias) == "sell", f"failed for {alias!r}"


def test_normalise_direction_on_bid_is_sell():
    """'on bid' / 'on the bid' negates the bare 'bid' default."""
    assert _normalise_direction("on bid") == "sell"
    assert _normalise_direction("on the bid") == "sell"


def test_normalise_direction_unknown():
    assert _normalise_direction("flipped") is None
    assert _normalise_direction("") is None
    assert _normalise_direction(None) is None


def test_normalise_product():
    assert _normalise_product("SR3") == "SR3"
    assert _normalise_product("sr3") == "SR3"
    assert _normalise_product("0Q") == "0Q"
    assert _normalise_product("ER") is None  # out of v2 scope
    assert _normalise_product(None) is None


def test_normalise_expiry():
    assert _normalise_expiry("U6") == "U6"
    assert _normalise_expiry("u6") == "U6"
    assert _normalise_expiry("Z7") == "Z7"
    assert _normalise_expiry("X9X") is None
    assert _normalise_expiry(None) is None


def test_normalise_size_int():
    assert _normalise_size(5000) == 5000
    assert _normalise_size("5000") == 5000
    assert _normalise_size("5,000") == 5000


def test_normalise_size_k_shorthand():
    assert _normalise_size("5k") == 5000
    assert _normalise_size("2.5k") == 2500


def test_normalise_size_garbage():
    assert _normalise_size("lots") is None
    assert _normalise_size(0) is None
    assert _normalise_size(None) is None


def test_normalise_price():
    assert _normalise_price("1") == 1.0
    assert _normalise_price(1.5) == 1.5
    assert _normalise_price("3.25") == 3.25
    assert _normalise_price("oops") is None


# ---------------------------------------------------------------------------
# Loader: integration tests
# ---------------------------------------------------------------------------

def test_load_full_row(tmp_path):
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, HEADERS_FULL, [
        ["2026-11-17", "SFRU6 96.43/96.50 cs ppr bought 5k at 1",
         "SR3", "U6", "96.43/96.50 cs", 5000, "bought", 1.0],
    ])
    rows = load_flow(f)
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2026-11-17"
    assert r["raw_note"] == "SFRU6 96.43/96.50 cs ppr bought 5k at 1"
    assert r["product"] == "SR3"
    assert r["expiry"] == "U6"
    assert r["structure"] == "96.43/96.50 cs"
    assert r["size"] == 5000
    assert r["direction"] == "buy"
    assert r["price"] == 1.0


def test_load_minimal_row(tmp_path):
    """Just date + raw_note → row emitted with structured fields None."""
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, ["date", "raw_note"], [
        ["2026-11-17", "Z6 calls bid all morning"],
    ])
    rows = load_flow(f)
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2026-11-17"
    assert r["raw_note"] == "Z6 calls bid all morning"
    assert r["product"] is None
    assert r["expiry"] is None
    assert r["direction"] is None
    assert r["size"] is None


def test_skip_row_missing_date(tmp_path):
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, HEADERS_FULL, [
        [None, "raw note here", "SR3", "U6", None, None, None, None],
        ["2026-11-17", "good row", "SR3", "U6", None, None, None, None],
    ])
    with pytest.warns(UserWarning, match="missing/bad date"):
        rows = load_flow(f)
    assert len(rows) == 1
    assert rows[0]["raw_note"] == "good row"


def test_skip_row_missing_raw_note(tmp_path):
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, HEADERS_FULL, [
        ["2026-11-17", None, "SR3", "U6", None, None, None, None],
        ["2026-11-18", "good", "SR3", "U6", None, None, None, None],
    ])
    with pytest.warns(UserWarning, match="missing raw_note"):
        rows = load_flow(f)
    assert len(rows) == 1
    assert rows[0]["raw_note"] == "good"


def test_unknown_product_to_none_not_drop(tmp_path):
    """Unknown product warns but row is still emitted with product=None."""
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, HEADERS_FULL, [
        ["2026-11-17", "ER colour", "ER", "U6", None, None, None, None],
    ])
    with pytest.warns(UserWarning, match="unknown product"):
        rows = load_flow(f)
    assert len(rows) == 1
    assert rows[0]["product"] is None
    assert rows[0]["raw_note"] == "ER colour"


def test_bad_expiry_to_none(tmp_path):
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, HEADERS_FULL, [
        ["2026-11-17", "x", "SR3", "X9X", None, None, None, None],
    ])
    with pytest.warns(UserWarning, match="bad expiry"):
        rows = load_flow(f)
    assert rows[0]["expiry"] is None


def test_direction_normalisation_in_column(tmp_path):
    """Trader-speak in the structured direction column → buy/sell."""
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, HEADERS_FULL, [
        ["2026-11-17", "n1", "SR3", "U6", None, None, "paid",    None],
        ["2026-11-17", "n2", "SR3", "U6", None, None, "lifted",  None],
        ["2026-11-17", "n3", "SR3", "U6", None, None, "hit",     None],
        ["2026-11-17", "n4", "SR3", "U6", None, None, "offered", None],
        ["2026-11-17", "n5", "SR3", "U6", None, None, "on bid",  None],
    ])
    rows = load_flow(f)
    assert [r["direction"] for r in rows] == ["buy", "buy", "sell", "sell", "sell"]


def test_load_client_trades_uses_same_loader(tmp_path):
    """load_flow and load_client_trades produce identical row dicts."""
    f1 = tmp_path / "flow.xlsx"
    f2 = tmp_path / "client_trades.xlsx"
    rows_data = [["2026-11-17", "raw", "SR3", "U6", None, None, "buy", None]]
    _write_xlsx(f1, HEADERS_FULL, rows_data)
    _write_xlsx(f2, HEADERS_FULL, rows_data)
    assert load_flow(f1) == load_client_trades(f2)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FlowLoaderError, match="file not found"):
        load_flow(tmp_path / "missing.xlsx")


def test_missing_required_column_raises(tmp_path):
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, ["date"], [["2026-11-17"]])
    with pytest.raises(FlowLoaderError, match="raw_note"):
        load_flow(f)


def test_empty_sheet_returns_empty_list(tmp_path):
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, HEADERS_FULL, [])
    with pytest.warns(UserWarning, match="no data rows"):
        rows = load_flow(f)
    assert rows == []


def test_size_k_shorthand_in_excel(tmp_path):
    f = tmp_path / "flow.xlsx"
    _write_xlsx(f, HEADERS_FULL, [
        ["2026-11-17", "5k buy", "SR3", "U6", None, "5k", "bought", None],
    ])
    rows = load_flow(f)
    assert rows[0]["size"] == 5000


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------

def test_filter_by_window():
    rows = [
        {"date": "2026-11-10", "raw_note": "before"},
        {"date": "2026-11-17", "raw_note": "in"},
        {"date": "2026-11-18", "raw_note": "in"},
        {"date": "2026-11-21", "raw_note": "in (end)"},
        {"date": "2026-11-25", "raw_note": "after"},
    ]
    filtered = filter_rows_to_window(rows,
                                     date(2026, 11, 17),
                                     date(2026, 11, 21))
    assert len(filtered) == 3
    assert all("in" in r["raw_note"] for r in filtered)


def test_filter_inclusive_both_ends():
    rows = [{"date": "2026-11-17", "raw_note": "x"}]
    assert filter_rows_to_window(rows, date(2026, 11, 17), date(2026, 11, 17)) == rows
