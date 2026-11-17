"""Combine fetch results into tables and serialise them as an Excel workbook
(prices and volume shown with Indian digit grouping)."""

from __future__ import annotations

import io
import re
from datetime import date
from typing import Dict, Iterable, List, Sequence

import pandas as pd

from .candles import CANDLE_COLUMNS, STATUS_OK, FetchResult
from .formatting import EXCEL_INDIAN_2DP, EXCEL_INDIAN_INT, strike_label

DATA_COLUMNS = ["underlying", "expiry", "strike", "option_type", "symbol", *CANDLE_COLUMNS]
SUMMARY_COLUMNS = ["underlying", "expiry", "file_symbol", "symbol", "strike", "option_type", "status",
                   "candles", "first_candle", "last_candle", "total_volume", "message", "notes",
                   "warnings"]
SUMMARY_SHEET = "summary"

_DATA_FORMATS = {
    "expiry": "DD-MMM-YYYY", "datetime": "DD-MM-YYYY HH:MM",
    "open": EXCEL_INDIAN_2DP, "high": EXCEL_INDIAN_2DP, "low": EXCEL_INDIAN_2DP,
    "close": EXCEL_INDIAN_2DP, "volume": EXCEL_INDIAN_INT,
}
_SUMMARY_FORMATS = {
    "expiry": "DD-MMM-YYYY", "first_candle": "DD-MM-YYYY HH:MM", "last_candle": "DD-MM-YYYY HH:MM",
    "total_volume": EXCEL_INDIAN_INT,
}


def combine(results: Iterable[FetchResult]) -> pd.DataFrame:
    """All candles in one long table, ordered by underlying, expiry, strike, type, time."""
    parts: List[pd.DataFrame] = []
    for result in results:
        if result.status != STATUS_OK or result.frame.empty:
            continue
        part = result.frame.copy()
        contract = result.contract
        part.insert(0, "symbol", contract.symbol)
        part.insert(0, "option_type", contract.option_type)
        part.insert(0, "strike", contract.strike)
        part.insert(0, "expiry", contract.expiry)
        part.insert(0, "underlying", contract.underlying)
        parts.append(part)
    if not parts:
        return pd.DataFrame(columns=DATA_COLUMNS)
    data = pd.concat(parts, ignore_index=True)
    return data.sort_values(["underlying", "expiry", "strike", "option_type", "datetime"]
                            ).reset_index(drop=True)[DATA_COLUMNS]


def split_by_underlying(data: pd.DataFrame, underlyings: Sequence[str]) -> Dict[str, pd.DataFrame]:
    """{underlying: its rows}, one entry per underlying asked for — an index whose
    contracts all came back empty still gets its (empty) sheet."""
    return {u: data[data["underlying"] == u].reset_index(drop=True) for u in underlyings}


def summary_frame(results: Iterable[FetchResult], issues: Iterable = ()) -> pd.DataFrame:
    """One row per contract fetched, plus one per file symbol NOT fetched (issues)."""
    rows = []
    for result in results:
        frame = result.frame
        has_rows = result.status == STATUS_OK and not frame.empty
        contract = result.contract
        rows.append({
            "underlying": contract.underlying,
            "expiry": contract.expiry,
            "file_symbol": " | ".join(result.source_symbols),
            "symbol": contract.symbol,
            "strike": contract.strike,
            "option_type": contract.option_type,
            "status": result.status,
            "candles": len(frame) if has_rows else 0,
            "first_candle": frame["datetime"].iloc[0] if has_rows else pd.NaT,
            "last_candle": frame["datetime"].iloc[-1] if has_rows else pd.NaT,
            "total_volume": float(frame["volume"].sum()) if has_rows else None,
            "message": result.message,
            "notes": "; ".join(result.notes),
            "warnings": "; ".join(result.warnings),
        })
    for issue in issues:
        rows.append({
            "underlying": issue.underlying, "expiry": issue.expiry, "file_symbol": issue.file_symbol,
            "symbol": "", "strike": issue.strike, "option_type": issue.option_type,
            "status": issue.status, "candles": 0, "first_candle": pd.NaT, "last_candle": pd.NaT,
            "total_volume": None, "message": issue.message, "notes": f"{issue.rows} row(s) in file",
            "warnings": "",
        })
    summary = pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
    if not summary["file_symbol"].astype(bool).any():
        summary = summary.drop(columns=["file_symbol"])        # manual mode: no file
    return summary


def file_stem(underlying: str, expiry: date, day: date, strikes: Iterable[float]) -> str:
    strikes = sorted(set(strikes))
    span = (strike_label(strikes[0]) if len(strikes) == 1
            else f"{strike_label(strikes[0])}-{strike_label(strikes[-1])}" if strikes else "none")
    return f"{underlying}_{expiry:%d%b%y}".upper() + f"_{span}_{day:%Y-%m-%d}_1min"


def position_file_stem(upload_name: str, day: date) -> str:
    base = re.sub(r"\.[A-Za-z0-9]+$", "", upload_name)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "positions"
    return f"{base}_{day:%Y-%m-%d}_1min"


def _sheet_name(name: str) -> str:
    """Excel sheet names: max 31 chars, none of []:*?/\\ ."""
    return re.sub(r"[\[\]:*?/\\]", "_", str(name))[:31] or "sheet"


def _format_sheet(sheet, frame: pd.DataFrame, formats: dict) -> None:
    sheet.freeze_panes = "A2"
    for position, column in enumerate(frame.columns, start=1):
        letter = sheet.cell(row=1, column=position).column_letter
        sheet.column_dimensions[letter].width = max(12, min(48, len(str(column)) + 4))
        number_format = formats.get(column)
        if number_format:
            for (cell,) in sheet.iter_rows(min_row=2, min_col=position, max_col=position):
                cell.number_format = number_format


def to_xlsx_bytes(sheets: Dict[str, pd.DataFrame], summary: pd.DataFrame) -> bytes:
    """Data sheets in the given order, then the summary sheet."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            sheet_name = _sheet_name(name)
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
            _format_sheet(writer.sheets[sheet_name], frame, _DATA_FORMATS)
        summary.to_excel(writer, sheet_name=SUMMARY_SHEET, index=False)
        _format_sheet(writer.sheets[SUMMARY_SHEET], summary, _SUMMARY_FORMATS)
    return buffer.getvalue()
