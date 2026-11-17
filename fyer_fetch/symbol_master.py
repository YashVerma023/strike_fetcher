"""Fyers symbol master: the list of LIVE contracts, used to fill the expiry and
strike dropdowns and to look up each contract's exact Fyers symbol.

Why the master file and not the option-chain API (which the DOR uses):
* It covers NSE_FO, BSE_FO and MCX_COM the same way; option-chain MCX support
  is not documented.
* It lists every strike, not the 50 either side of ATM the chain returns.
* It is one download per segment per day instead of API calls.

Symbols are looked up, never built: weekly and monthly symbols are spelled
differently (NSE:NIFTY26N1717350CE vs NSE:BANKNIFTY26NOV31500CE) and a wrong
guess can resolve to a real but different contract.

File layout (no header row, 21 columns) per Fyers' "Symbol Master File" docs.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd
import requests

from .settings import SymbolMasterSettings
from .timeutil import IST_OFFSET_S

logger = logging.getLogger(__name__)

MASTER_COLUMNS = (
    "fytoken", "symbol_details", "exchange_instrument_type", "minimum_lot_size",
    "tick_size", "isin", "trading_session", "last_update_date", "expiry_date",
    "symbol_ticker", "exchange", "segment", "scrip_code", "underlying_symbol",
    "underlying_scrip_code", "strike_price", "option_type", "underlying_fytoken",
    "reserved_1", "reserved_2", "reserved_3",
)
OPTION_TYPES = ("CE", "PE")
STRIKE_DECIMALS = 4          # strikes are compared after rounding (e.g. 102.5 vs 102.50000001)
_CACHE_NAME = re.compile(r"^(?P<segment>[A-Z_]+)_(?P<day>\d{4}-\d{2}-\d{2})\.csv$")


class SymbolMasterError(Exception):
    """The symbol master could not be downloaded, read, or understood."""


@dataclass(frozen=True)
class MasterFile:
    segment: str
    path: Path
    as_of: date
    is_stale: bool          # True when today's download failed and an older cached file is used


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    expiry: date
    strike: float
    option_type: str
    lot_size: Optional[int]


# --------------------------------------------------------------------------- download / cache

def _cached_files(cache_dir: Path, segment: str) -> List[tuple]:
    """[(day, path), ...] cached for this segment, newest first."""
    found = []
    if not cache_dir.exists():
        return found
    for path in cache_dir.glob(f"{segment}_*.csv"):
        match = _CACHE_NAME.match(path.name)
        if not match or match.group("segment") != segment or path.stat().st_size == 0:
            continue
        try:
            found.append((date.fromisoformat(match.group("day")), path))
        except ValueError:
            continue
    return sorted(found, reverse=True)


def _prune(cache_dir: Path, segment: str, keep: int) -> None:
    for _, path in _cached_files(cache_dir, segment)[keep:]:
        try:
            path.unlink()
            logger.info("removed old symbol master %s", path.name)
        except OSError as exc:
            logger.warning("could not remove old symbol master %s (%s)", path.name, exc)


def _download(url: str, target: Path, settings: SymbolMasterSettings) -> None:
    last_error = "no attempt made"
    for attempt in range(1, settings.download_retries + 1):
        try:
            response = requests.get(url, timeout=settings.download_timeout_s)
            response.raise_for_status()
            if not response.content.strip():
                raise SymbolMasterError("server returned an empty file")
            partial = target.with_suffix(".part")
            partial.write_bytes(response.content)
            partial.replace(target)          # atomic: a half-written file is never read
            logger.info("downloaded %s (%s bytes) -> %s", url, f"{len(response.content):,}", target)
            return
        except (requests.exceptions.RequestException, SymbolMasterError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("symbol master download %d/%d failed: %s", attempt,
                           settings.download_retries, last_error)
            if attempt < settings.download_retries:
                time.sleep(settings.retry_backoff_s * attempt)
    raise SymbolMasterError(f"could not download {url} after {settings.download_retries} "
                            f"attempt(s): {last_error}")


def ensure_master_file(segment: str, settings: SymbolMasterSettings, today: date) -> MasterFile:
    """Today's master for `segment`, downloading it once per day.

    If today's download fails, the newest cached file is used and flagged stale
    (strikes added since then will be missing); with no cache at all it raises."""
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    target = settings.cache_dir / f"{segment}_{today:%Y-%m-%d}.csv"
    if target.exists() and target.stat().st_size > 0:
        return MasterFile(segment, target, today, is_stale=False)

    url = settings.url_template.format(segment=segment)
    try:
        _download(url, target, settings)
    except SymbolMasterError as exc:
        cached = _cached_files(settings.cache_dir, segment)
        if not cached:
            raise SymbolMasterError(f"{exc}. No cached copy of {segment} is available either.") from exc
        day, path = cached[0]
        logger.warning("using cached %s from %s because today's download failed", path.name, day)
        return MasterFile(segment, path, day, is_stale=True)

    _prune(settings.cache_dir, segment, settings.cache_keep_files)
    return MasterFile(segment, target, today, is_stale=False)


# --------------------------------------------------------------------------- parse

def parse_master(path: Path, underlyings: Iterable[str]) -> pd.DataFrame:
    """Option contracts for `underlyings`, one row per contract:
    symbol, underlying, expiry (date, IST), strike (float), option_type, lot_size."""
    wanted = {str(u).strip().upper() for u in underlyings}
    try:
        raw = pd.read_csv(path, header=None, dtype=str, keep_default_na=False, encoding="utf-8")
    except (OSError, UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise SymbolMasterError(f"{path.name} could not be read ({exc})") from exc
    if raw.shape[1] != len(MASTER_COLUMNS):
        raise SymbolMasterError(f"{path.name}: expected {len(MASTER_COLUMNS)} columns, found "
                                f"{raw.shape[1]} — Fyers may have changed the file layout")
    raw.columns = list(MASTER_COLUMNS)

    for column in ("symbol_ticker", "underlying_symbol", "option_type"):
        raw[column] = raw[column].str.strip()
    raw["underlying_symbol"] = raw["underlying_symbol"].str.upper()
    raw["option_type"] = raw["option_type"].str.upper()

    frame = raw[raw["option_type"].isin(OPTION_TYPES) & raw["underlying_symbol"].isin(wanted)].copy()
    frame["strike"] = pd.to_numeric(frame["strike_price"], errors="coerce").round(STRIKE_DECIMALS)
    frame["expiry_epoch"] = pd.to_numeric(frame["expiry_date"], errors="coerce")
    frame["lot_size"] = pd.to_numeric(frame["minimum_lot_size"], errors="coerce").astype("Int64")

    bad = (frame["strike"].isna() | (frame["strike"] <= 0) | frame["expiry_epoch"].isna()
           | (frame["symbol_ticker"] == ""))
    if bad.any():
        logger.warning("%s: dropped %d option row(s) with an unreadable strike, expiry or symbol",
                       path.name, int(bad.sum()))
        frame = frame[~bad]

    # expiry epoch -> calendar date in IST (MCX options expire 23:55 IST, so a
    # UTC date would land on the wrong day for some rows)
    frame["expiry"] = pd.to_datetime(frame["expiry_epoch"].astype("int64") + IST_OFFSET_S,
                                     unit="s").dt.date

    out = frame.rename(columns={"symbol_ticker": "symbol", "underlying_symbol": "underlying"})[
        ["symbol", "underlying", "expiry", "strike", "option_type", "lot_size"]]

    dupes = out.duplicated(["underlying", "expiry", "strike", "option_type"], keep=False)
    if dupes.any():
        logger.warning("%s: %d row(s) share underlying/expiry/strike/type — those contracts will "
                       "be refused as ambiguous", path.name, int(dupes.sum()))

    missing = wanted - set(out["underlying"].unique())
    if missing:
        logger.warning("%s: no option contracts found for %s", path.name, ", ".join(sorted(missing)))
    logger.info("%s: %d option contracts for %s", path.name, len(out), ", ".join(sorted(wanted)))
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- lookup

class SymbolMaster:
    """Lookups over the frame parse_master() returns."""

    def __init__(self, frame: pd.DataFrame):
        self._frame = frame
        # "NIFTY26N1726750CE" -> row positions, for files that carry the ticker
        # without its "NSE:" / "BSE:" / "MCX:" prefix
        self._by_ticker = {}
        for position, symbol in enumerate(frame["symbol"]):
            self._by_ticker.setdefault(str(symbol).split(":", 1)[-1], []).append(position)

    def expiries(self, underlying: str, on_or_after: date) -> List[date]:
        rows = self._frame[(self._frame["underlying"] == underlying)
                           & (self._frame["expiry"] >= on_or_after)]
        return sorted(set(rows["expiry"]))

    def strikes(self, underlying: str, expiry: date) -> List[float]:
        rows = self._frame[(self._frame["underlying"] == underlying)
                           & (self._frame["expiry"] == expiry)]
        return sorted(set(float(s) for s in rows["strike"]))

    def find_ticker(self, ticker: str) -> Optional[OptionContract]:
        """The contract whose Fyers symbol, without its exchange prefix, is `ticker`."""
        positions = self._by_ticker.get(ticker.strip().upper(), [])
        if not positions:
            return None
        if len(positions) > 1:
            symbols = ", ".join(self._frame.iloc[positions]["symbol"])
            raise SymbolMasterError(f"{ticker} matches {len(positions)} symbols ({symbols}) — refusing to guess")
        return self._to_contract(self._frame.iloc[positions[0]])

    @staticmethod
    def _to_contract(row: pd.Series) -> OptionContract:
        lot = row["lot_size"]
        return OptionContract(symbol=str(row["symbol"]), underlying=str(row["underlying"]),
                              expiry=row["expiry"], strike=float(row["strike"]),
                              option_type=str(row["option_type"]),
                              lot_size=None if pd.isna(lot) else int(lot))

    def contract(self, underlying: str, expiry: date, strike: float,
                 option_type: str) -> Optional[OptionContract]:
        """The contract, or None if the master does not list it. Raises if ambiguous."""
        rows = self._frame[(self._frame["underlying"] == underlying)
                           & (self._frame["expiry"] == expiry)
                           & (self._frame["strike"] == round(float(strike), STRIKE_DECIMALS))
                           & (self._frame["option_type"] == option_type)]
        if rows.empty:
            return None
        if len(rows) > 1:
            raise SymbolMasterError(f"{underlying} {expiry:%d-%b-%Y} {strike:g} {option_type} matches "
                                    f"{len(rows)} symbols ({', '.join(rows['symbol'])}) — refusing to guess")
        return self._to_contract(rows.iloc[0])
