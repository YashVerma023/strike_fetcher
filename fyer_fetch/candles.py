"""Fetch one contract's 1-minute candles for one day, then clean and check them.

Clean-up (applied, and reported as notes):
* Same-day duplicates: Fyers returns the current day's session twice (the live
  feed appended to the stored one; found in the DOR, 750 rows for 375 minutes).
  The LAST row per minute is kept, as the more complete one.
* The candle for the current, still-forming minute is dropped when the date is today.

Checks (reported as warnings, rows are KEPT so nothing is hidden):
* high/low inconsistent with open/close, non-positive prices, negative volume.
Rows that cannot be used at all (malformed, unreadable prices, wrong date) are
dropped and counted in the warnings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Sequence, Tuple

import pandas as pd

from .fyers_api import FyersDataClient, is_auth_error
from .symbol_master import OptionContract
from .timeutil import IST_OFFSET_S, now_ist

logger = logging.getLogger(__name__)

RESOLUTION = "1"                         # 1-minute candles
PRICE_COLUMNS = ("open", "high", "low", "close")
CANDLE_COLUMNS = ("datetime",) + PRICE_COLUMNS + ("volume",)

STATUS_OK = "ok"
STATUS_NO_DATA = "no data"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


@dataclass
class FetchResult:
    contract: OptionContract
    day: date
    status: str
    message: str = ""
    frame: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=list(CANDLE_COLUMNS)))
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    auth_failed: bool = False
    source_symbols: Tuple[str, ...] = ()      # position-file mode: the file's spelling(s) of this contract


def history_payload(symbol: str, day: date) -> Dict[str, Any]:
    return {"symbol": symbol, "resolution": RESOLUTION, "date_format": "1",
            "range_from": day.isoformat(), "range_to": day.isoformat(), "cont_flag": "1"}


def normalise_candles(raw: Sequence[Any], day: date,
                      now: datetime) -> Tuple[pd.DataFrame, List[str], List[str]]:
    """(frame, notes, warnings). `now` must be timezone-aware IST.
    Output datetimes are naive IST (Excel cannot store timezone-aware values)."""
    notes: List[str] = []
    warnings: List[str] = []

    rows = [list(r[:6]) for r in raw if isinstance(r, (list, tuple)) and len(r) >= 6]
    if len(rows) < len(raw):
        warnings.append(f"{len(raw) - len(rows)} malformed candle row(s) dropped")
    frame = pd.DataFrame(rows, columns=["epoch", *PRICE_COLUMNS, "volume"])
    if frame.empty:
        return pd.DataFrame(columns=list(CANDLE_COLUMNS)), notes, warnings

    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    unreadable = frame[["epoch", *PRICE_COLUMNS]].isna().any(axis=1)
    if unreadable.any():
        warnings.append(f"{int(unreadable.sum())} candle(s) with an unreadable time or price dropped")
        frame = frame[~unreadable]
    if frame["volume"].isna().any():
        warnings.append(f"{int(frame['volume'].isna().sum())} candle(s) have no volume value")

    before = len(frame)
    frame = frame.drop_duplicates("epoch", keep="last")
    if len(frame) < before:
        notes.append(f"{before - len(frame)} repeated minute(s) removed (Fyers repeats the same-day "
                     f"session; the later copy is kept)")

    frame["datetime"] = pd.to_datetime(frame["epoch"].astype("int64") + IST_OFFSET_S, unit="s")

    off_day = frame["datetime"].dt.date != day
    if off_day.any():
        warnings.append(f"{int(off_day.sum())} candle(s) dated outside {day:%d-%b-%Y} dropped")
        frame = frame[~off_day]

    if day == now.date():
        forming_from = pd.Timestamp(now.replace(second=0, microsecond=0, tzinfo=None))
        forming = frame["datetime"] >= forming_from
        if forming.any():
            notes.append(f"candle for the still-forming minute {forming_from:%H:%M} dropped")
            frame = frame[~forming]

    frame = frame.sort_values("datetime")

    body_high = frame[["open", "close"]].max(axis=1)
    body_low = frame[["open", "close"]].min(axis=1)
    inconsistent = (frame["high"] < body_high) | (frame["low"] > body_low) | (frame["high"] < frame["low"])
    if inconsistent.any():
        warnings.append(f"{int(inconsistent.sum())} candle(s) where high/low do not contain open/close")
    non_positive = (frame[list(PRICE_COLUMNS)] <= 0).any(axis=1)
    if non_positive.any():
        warnings.append(f"{int(non_positive.sum())} candle(s) with a zero or negative price")
    negative_volume = frame["volume"] < 0
    if negative_volume.any():
        warnings.append(f"{int(negative_volume.sum())} candle(s) with negative volume")

    volume = frame["volume"]
    if volume.notna().all() and (volume % 1 == 0).all():
        frame["volume"] = volume.astype("int64")

    return frame[list(CANDLE_COLUMNS)].reset_index(drop=True), notes, warnings


def fetch_contract(client: FyersDataClient, contract: OptionContract, day: date,
                   now: datetime = None) -> FetchResult:
    now = now or now_ist()
    resp = client.history(history_payload(contract.symbol, day))

    if resp.get("s") == "error":
        message = f"Fyers error {resp.get('code')}: {resp.get('message') or 'no message'}"
        logger.error("%s %s: %s", contract.symbol, day, message)
        return FetchResult(contract, day, STATUS_ERROR, message, auth_failed=is_auth_error(resp))

    raw = resp.get("candles") or []
    if not raw:
        message = ("no candles — not a trading day, the contract was not yet listed on that "
                   "date, or it did not trade")
        logger.info("%s %s: %s (s=%s)", contract.symbol, day, message, resp.get("s"))
        return FetchResult(contract, day, STATUS_NO_DATA, message)

    frame, notes, warnings = normalise_candles(raw, day, now)
    for text in warnings:
        logger.warning("%s %s: %s", contract.symbol, day, text)
    if frame.empty:
        return FetchResult(contract, day, STATUS_NO_DATA, "no usable candles after checks",
                           notes=notes, warnings=warnings)

    logger.info("%s %s: %d candles %s-%s", contract.symbol, day, len(frame),
                f"{frame['datetime'].iloc[0]:%H:%M}", f"{frame['datetime'].iloc[-1]:%H:%M}")
    return FetchResult(contract, day, STATUS_OK, f"{len(frame)} candles", frame=frame,
                       notes=notes, warnings=warnings)
