"""Position-file mode: read a compiled position CSV and turn its Symbol column
into the LIVE option contracts to fetch.

Symbol spellings seen in compiled position files (all mean the same contract):

    NIFTY26N1726750CE            Fyers ticker, weekly: YY + month code (1-9, O, N, D) + DD
    NIFTY26DEC25000CE            Fyers ticker, monthly: YY + MON
    SENSEX 12NOV2026 CE 81700    spaced: DDMONYYYY, type, strike
    SENSEX 12NOV26 84500 CE      spaced: DDMONYY, strike, type

Every parsed symbol is matched against today's Fyers symbol master; nothing is
fetched on the strength of the parse alone. Tickers are matched exactly
(exchange prefix added by the master), and the match is cross-checked against
the parse. Outcome per unique symbol:

    fetched   live contract found in the master
    error     expired, not in today's master, ambiguous, or parse/master mismatch
    skipped   not an option (stock, future) or an underlying not in settings.json
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Mapping, Optional, Set, Tuple

import pandas as pd

from .candles import STATUS_ERROR, STATUS_SKIPPED
from .formatting import strike_label
from .symbol_master import OptionContract, SymbolMaster, SymbolMasterError

logger = logging.getLogger(__name__)

SYMBOL_COLUMN = "symbol"          # matched case-insensitively, surrounding spaces ignored
SUPPORTED_EXTENSIONS = ("csv", "xlsx")
_ENCODINGS = ("utf-8-sig", "cp1252")

_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}
_WEEKLY_MONTH = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}
_NUM = r"\d+(?:\.\d+)?"
_TICKER_WEEKLY = re.compile(
    rf"^(?P<u>[A-Z&]+?)(?P<yy>\d{{2}})(?P<m>[1-9OND])(?P<dd>\d{{2}})(?P<k>{_NUM})(?P<t>CE|PE)$")
_TICKER_MONTHLY = re.compile(
    rf"^(?P<u>[A-Z&]+?)(?P<yy>\d{{2}})(?P<mon>{'|'.join(_MONTHS)})(?P<k>{_NUM})(?P<t>CE|PE)$")
_SPACED_TYPE_FIRST = re.compile(
    rf"^(?P<u>[A-Z&]+) (?P<exp>\d{{1,2}}[A-Z]{{3}}\d{{4}}) (?P<t>CE|PE) (?P<k>{_NUM})$")
_SPACED_STRIKE_FIRST = re.compile(
    rf"^(?P<u>[A-Z&]+) (?P<exp>\d{{1,2}}[A-Z]{{3}}\d{{2}}) (?P<k>{_NUM}) (?P<t>CE|PE)$")


class PositionFileError(Exception):
    """The uploaded file cannot be read or has no Symbol column."""


@dataclass(frozen=True)
class ParsedSymbol:
    raw: str
    is_ticker: bool                 # Fyers ticker spelling (matched exactly against the master)
    underlying: str
    expiry: Optional[date]          # None for a monthly ticker: the symbol gives only the month
    expiry_month: Tuple[int, int]   # (year, month)
    strike: float
    option_type: str


@dataclass(frozen=True)
class SymbolIssue:
    """A unique file symbol that will NOT be fetched, and why."""
    file_symbol: str
    rows: int
    status: str                     # STATUS_ERROR or STATUS_SKIPPED
    message: str
    underlying: str = ""
    expiry: Optional[date] = None
    strike: Optional[float] = None
    option_type: str = ""


@dataclass
class PositionSymbols:
    file_rows: int
    blank_symbol_rows: int
    counts: pd.Series                       # normalised symbol -> number of rows


@dataclass
class Resolution:
    contracts: List[Tuple[OptionContract, Tuple[str, ...]]] = field(default_factory=list)
    issues: List[SymbolIssue] = field(default_factory=list)


def normalise_symbol(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().upper())


def parse_symbol(text: str) -> Optional[ParsedSymbol]:
    """None when the text is not an option symbol in a known spelling."""
    raw = normalise_symbol(text)
    try:
        match = _TICKER_WEEKLY.match(raw)
        if match:
            year = 2000 + int(match["yy"])
            expiry = date(year, _WEEKLY_MONTH[match["m"]], int(match["dd"]))
            return ParsedSymbol(raw, True, match["u"], expiry, (expiry.year, expiry.month),
                                float(match["k"]), match["t"])
        match = _TICKER_MONTHLY.match(raw)
        if match:
            month = (2000 + int(match["yy"]), _MONTHS[match["mon"]])
            return ParsedSymbol(raw, True, match["u"], None, month, float(match["k"]), match["t"])
        for pattern, fmt in ((_SPACED_TYPE_FIRST, "%d%b%Y"), (_SPACED_STRIKE_FIRST, "%d%b%y")):
            match = pattern.match(raw)
            if match:
                expiry = datetime.strptime(match["exp"], fmt).date()
                return ParsedSymbol(raw, False, match["u"], expiry, (expiry.year, expiry.month),
                                    float(match["k"]), match["t"])
    except ValueError:              # e.g. day 31 in a 30-day month: not a real expiry
        return None
    return None


def _read_csv(content: bytes) -> pd.DataFrame:
    for encoding in _ENCODINGS:
        try:
            return pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, encoding=encoding)
        except UnicodeDecodeError:
            continue
        except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise PositionFileError(f"the file could not be read as CSV ({exc})") from exc
    raise PositionFileError(f"the file is not in a supported text encoding ({', '.join(_ENCODINGS)})")


def _read_xlsx(content: bytes) -> pd.DataFrame:
    """First sheet only, header in row 1."""
    try:
        return pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=str, engine="openpyxl")
    except (zipfile.BadZipFile, ValueError, KeyError, OSError) as exc:
        raise PositionFileError(f"the file could not be read as an Excel workbook ({exc})") from exc


def read_position_file(content: bytes, filename: str) -> PositionSymbols:
    """Read the Symbol column of a .csv, or of the FIRST sheet of an .xlsx."""
    if not content or not content.strip():
        raise PositionFileError("the file is empty")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension == "csv":
        frame = _read_csv(content)
    elif extension == "xlsx":
        frame = _read_xlsx(content)
    else:
        found = f"'.{extension}'" if extension else "a file with no extension"
        raise PositionFileError(f"unsupported file type {found} — use "
                                + " or ".join(f".{e}" for e in SUPPORTED_EXTENSIONS))

    columns = {str(c).strip().lower(): c for c in frame.columns}
    if SYMBOL_COLUMN not in columns:
        raise PositionFileError("no 'Symbol' column — columns found: "
                                + ", ".join(str(c) for c in frame.columns))
    # empty Excel cells arrive as NaN; they are blanks, not the text "nan"
    symbols = frame[columns[SYMBOL_COLUMN]].fillna("").map(normalise_symbol)
    blank = symbols == ""
    counts = symbols[~blank].value_counts()
    logger.info("position file: %d rows, %d blank symbols, %d unique symbols",
                len(frame), int(blank.sum()), len(counts))
    return PositionSymbols(file_rows=len(frame), blank_symbol_rows=int(blank.sum()), counts=counts)


def parse_all(counts: pd.Series) -> Dict[str, Optional[ParsedSymbol]]:
    return {symbol: parse_symbol(symbol) for symbol in counts.index}


def _describe(parsed: ParsedSymbol) -> str:
    when = f"{parsed.expiry:%d-%b-%Y}" if parsed.expiry else f"{date(*parsed.expiry_month, 1):%b-%Y} monthly"
    return f"{parsed.underlying} {when} {strike_label(parsed.strike)} {parsed.option_type}"


def _merge_spellings(issues: List[SymbolIssue]) -> List[SymbolIssue]:
    """One issue per contract: the same expired contract written three ways in
    the file is reported once, with all its spellings and their total row count."""
    merged: Dict[tuple, List[SymbolIssue]] = {}
    for issue in issues:
        key = ((issue.status, issue.message, issue.underlying, issue.expiry, issue.strike, issue.option_type)
               if issue.underlying else ("symbol", issue.file_symbol))
        merged.setdefault(key, []).append(issue)
    out = []
    for group in merged.values():
        first = group[0]
        out.append(SymbolIssue(" | ".join(sorted(i.file_symbol for i in group)), sum(i.rows for i in group),
                               first.status, first.message, first.underlying, first.expiry, first.strike,
                               first.option_type))
    return out


def resolve_symbols(counts: pd.Series, parsed: Mapping[str, Optional[ParsedSymbol]],
                    masters: Mapping[str, SymbolMaster], configured: Set[str],
                    today: date) -> Resolution:
    """Map each unique file symbol to a live contract, or to an issue saying why not.
    `masters` is keyed by underlying; it must cover every configured underlying in `parsed`."""
    found: Dict[str, Tuple[OptionContract, List[str]]] = {}
    issues: List[SymbolIssue] = []

    for symbol, rows in counts.items():
        item = parsed.get(symbol)
        rows = int(rows)
        if item is None:
            issues.append(SymbolIssue(symbol, rows, STATUS_SKIPPED,
                                      "not an option symbol (stock, future, or unrecognised spelling)"))
            continue
        base = dict(underlying=item.underlying, expiry=item.expiry, strike=item.strike,
                    option_type=item.option_type)
        if item.underlying not in configured:
            issues.append(SymbolIssue(symbol, rows, STATUS_SKIPPED,
                                      f"{item.underlying} is not in the configured instruments "
                                      f"(config/settings.json)", **base))
            continue

        expired = (item.expiry < today) if item.expiry else (item.expiry_month < (today.year, today.month))
        if expired:
            issues.append(SymbolIssue(symbol, rows, STATUS_ERROR,
                                      f"expired ({_describe(item)}) — Fyers' History API does not "
                                      f"serve expired contracts", **base))
            continue

        master = masters[item.underlying]
        try:
            contract = (master.find_ticker(item.raw) if item.is_ticker
                        else master.contract(item.underlying, item.expiry, item.strike, item.option_type))
        except SymbolMasterError as exc:
            issues.append(SymbolIssue(symbol, rows, STATUS_ERROR, str(exc), **base))
            continue
        if contract is None:
            issues.append(SymbolIssue(symbol, rows, STATUS_ERROR,
                                      f"{_describe(item)} is not in today's Fyers symbol master "
                                      f"(expired or never listed)", **base))
            continue

        # a ticker was matched by spelling alone; make sure it is the contract we parsed
        agrees = (contract.underlying == item.underlying and contract.option_type == item.option_type
                  and abs(contract.strike - item.strike) < 1e-6
                  and (item.expiry is None or contract.expiry == item.expiry)
                  and (contract.expiry.year, contract.expiry.month) == item.expiry_month)
        if not agrees:
            issues.append(SymbolIssue(symbol, rows, STATUS_ERROR,
                                      f"parsed as {_describe(item)} but Fyers lists {contract.symbol} "
                                      f"({contract.expiry:%d-%b-%Y} {strike_label(contract.strike)} "
                                      f"{contract.option_type}) — not fetched", **base))
            continue

        entry = found.setdefault(contract.symbol, (contract, []))
        entry[1].append(symbol)

    contracts = sorted(((c, tuple(sorted(spellings))) for c, spellings in found.values()),
                       key=lambda pair: (pair[0].underlying, pair[0].expiry, pair[0].strike,
                                         pair[0].option_type))
    issues = _merge_spellings(issues)
    issues.sort(key=lambda i: (i.status, i.underlying, i.expiry or date.min, i.strike or 0, i.file_symbol))
    logger.info("position file resolved: %d live contracts, %d errors, %d skipped", len(contracts),
                sum(i.status == STATUS_ERROR for i in issues), sum(i.status == STATUS_SKIPPED for i in issues))
    return Resolution(contracts=contracts, issues=issues)
