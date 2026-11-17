"""Fyers option 1-minute OHLC — Streamlit page.

    streamlit run app.py

Two ways to choose the contracts; the date always comes from the date picker:

* Pick strikes   — index/commodity, a LIVE expiry, CE/PE, one or more strikes.
* Position file  — upload a compiled position file (.csv, or the first sheet of
                   an .xlsx); every unique option in its Symbol column is fetched
                   (expired ones are reported, not fetched). The Excel download
                   has one sheet per index.

Live contracts only: Fyers' History API does not serve contracts whose expiry
has passed, so contracts are looked up in today's symbol master (live contracts).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd
import streamlit as st

from fyer_fetch.candles import STATUS_ERROR, STATUS_SKIPPED, FetchResult, fetch_contract
from fyer_fetch.credentials import CredentialsError, FyersCredentials, load_credentials
from fyer_fetch.export import (combine, file_stem, position_file_stem, split_by_underlying,
                               summary_frame, to_xlsx_bytes)
from fyer_fetch.formatting import indian_number, strike_label
from fyer_fetch.fyers_api import FyersClientError, FyersDataClient
from fyer_fetch.log_setup import configure_logging
from fyer_fetch.position_file import (SUPPORTED_EXTENSIONS, PositionFileError, SymbolIssue, parse_all,
                                      read_position_file, resolve_symbols)
from fyer_fetch.settings import FyersSettings, Settings, SettingsError, SymbolMasterSettings, load_settings
from fyer_fetch.symbol_master import OptionContract, SymbolMaster, SymbolMasterError, ensure_master_file, parse_master
from fyer_fetch.timeutil import now_ist

logger = logging.getLogger("fyer_fetch.app")

RUN_KEY = "last_run"
# Bump whenever the shape of the stored result changes. Streamlit keeps
# session_state across a code reload, so a result saved by an older version of
# this page would otherwise be rendered by newer code and fail (KeyError).
RUN_VERSION = 2
MODE_MANUAL = "Pick strikes"
MODE_FILE = "Position file"

ContractList = List[Tuple[OptionContract, Tuple[str, ...]]]


# --------------------------------------------------------------------------- cached loaders

@st.cache_data(show_spinner="Loading Fyers symbol master …", ttl=3600, max_entries=8)
def load_master(segment: str, underlyings: tuple, today_iso: str,
                _settings: SymbolMasterSettings) -> Dict[str, object]:
    """Parsed option contracts for one segment. Cached per segment per day;
    `_settings` is not part of the cache key (leading underscore)."""
    master_file = ensure_master_file(segment, _settings, date.fromisoformat(today_iso))
    frame = parse_master(master_file.path, underlyings)
    return {"frame": frame, "as_of": master_file.as_of, "is_stale": master_file.is_stale,
            "file": master_file.path.name}


@st.cache_resource(max_entries=2)
def get_client(client_id: str, access_token: str, _fyers_settings: FyersSettings,
               sdk_log_dir: str) -> FyersDataClient:
    """One SDK client per token; a new token (next morning) builds a new client."""
    return FyersDataClient(FyersCredentials(client_id, access_token, None), _fyers_settings,
                           Path(sdk_log_dir))


def master_for(settings: Settings, underlying: str, today: date) -> SymbolMaster:
    """Symbol master covering `underlying`; shows a warning if a stale cached copy is in use.
    Raises SymbolMasterError if no master is available at all."""
    instrument = settings.instrument(underlying)
    loaded = load_master(instrument.segment, settings.underlyings_in(instrument.segment),
                         today.isoformat(), settings.symbol_master)
    if loaded["is_stale"]:
        st.warning(f"Today's {instrument.segment} symbol master could not be downloaded — using the cached "
                   f"copy from {loaded['as_of']:%d-%b-%Y}. Contracts listed since then are missing.")
    return SymbolMaster(loaded["frame"])


# --------------------------------------------------------------------------- fetch

def execute(settings: Settings, creds: FyersCredentials, contracts: ContractList, day: date,
            issues: Sequence[SymbolIssue], request: dict, sheets_by_underlying: bool,
            stem: str) -> Optional[dict]:
    """Fetch every contract (paced), build tables and download files."""
    try:
        client = get_client(creds.client_id, creds.access_token, settings.fyers,
                            str(settings.log_dir / "fyers_sdk"))
    except FyersClientError as exc:
        st.error(str(exc))
        return None

    logger.info("fetch mode=%s date=%s contracts=%d not_fetched=%d", request["mode"], day,
                len(contracts), len(issues))
    results: List[FetchResult] = []
    progress = st.progress(0.0, text="Starting …")
    auth_failed = False
    for position, (contract, spellings) in enumerate(contracts, start=1):
        if auth_failed:
            results.append(FetchResult(contract, day, STATUS_SKIPPED,
                                       "skipped — Fyers rejected the access token",
                                       source_symbols=spellings))
            continue
        progress.progress((position - 1) / len(contracts),
                          text=f"Fetching {contract.symbol} ({position}/{len(contracts)})")
        try:
            result = fetch_contract(client, contract, day, now=now_ist())
        except Exception:  # noqa: BLE001 — UI boundary: one bad contract must not lose the others
            logger.exception("unexpected error fetching %s for %s", contract.symbol, day)
            result = FetchResult(contract, day, STATUS_ERROR, "unexpected error — see the log file")
        result.source_symbols = spellings
        results.append(result)
        auth_failed = result.auth_failed
    progress.empty()

    summary = summary_frame(results, issues)
    data = combine(results)
    if sheets_by_underlying:
        underlyings = list(dict.fromkeys(c.underlying for c, _ in contracts))
        sheets = split_by_underlying(data, underlyings)
    else:
        sheets = {"ohlc_1min": data}
    return {
        "version": RUN_VERSION,
        "request": request,
        "fetched_at": now_ist(),
        "results": results,
        "issues": list(issues),
        "auth_failed": auth_failed,
        "summary": summary,
        "sheets": sheets,
        "stem": stem,
        "xlsx": to_xlsx_bytes(sheets, summary),
    }


def fetch_button(settings: Settings, count: int, label: str = "Fetch 1-min OHLC") -> bool:
    st.caption(f"{count} contract(s) · about {indian_number(count * settings.fyers.min_call_interval_s)}s "
               f"at Fyers' pacing ({settings.fyers.min_call_interval_s}s per call)")
    return st.button(label, type="primary", disabled=count == 0)


# --------------------------------------------------------------------------- section 1: manual

def manual_section(settings: Settings, creds: FyersCredentials, today: date, day: date) -> None:
    col_inst, col_exp, col_type = st.columns(3)
    underlying = col_inst.selectbox("Index / commodity", [i.name for i in settings.instruments])
    try:
        master = master_for(settings, underlying, today)
    except SymbolMasterError as exc:
        st.error(f"Symbol master unavailable: {exc}")
        st.stop()

    # Only expiries that have not passed: those are the contracts Fyers' History API serves.
    expiries = master.expiries(underlying, on_or_after=today)
    if not expiries:
        st.error(f"No live {underlying} option expiries in today's symbol master.")
        st.stop()
    expiry = col_exp.selectbox("Expiry", expiries, format_func=lambda d: f"{d:%d-%b-%Y (%a)}")
    option_types = col_type.multiselect("Option type", ["CE", "PE"], default=["CE"])

    strikes = master.strikes(underlying, expiry)
    selected = sorted(st.multiselect(f"Strikes ({len(strikes)} listed for {expiry:%d-%b-%Y})", strikes,
                                     format_func=strike_label,
                                     placeholder="Type to search, pick one or more"))

    if not fetch_button(settings, len(selected) * len(option_types)):
        return

    contracts: ContractList = []
    issues: List[SymbolIssue] = []
    for strike in selected:
        for option_type in option_types:
            label = f"{underlying} {expiry:%d-%b-%Y} {strike_label(strike)} {option_type}"
            try:
                contract = master.contract(underlying, expiry, strike, option_type)
            except SymbolMasterError as exc:
                issues.append(SymbolIssue("", 0, STATUS_ERROR, str(exc), underlying, expiry, strike,
                                          option_type))
                continue
            if contract is None:
                issues.append(SymbolIssue("", 0, STATUS_ERROR, f"{label} is not in the symbol master",
                                          underlying, expiry, strike, option_type))
            else:
                contracts.append((contract, ()))

    request = {"mode": MODE_MANUAL, "day": day,
               "title": (f"**{underlying}** · expiry **{expiry:%d-%b-%Y}** · {', '.join(option_types)} · "
                         f"strikes {', '.join(strike_label(s) for s in selected)}")}
    stem = file_stem(underlying, expiry, day, selected)
    run = execute(settings, creds, contracts, day, issues, request, sheets_by_underlying=False, stem=stem)
    if run is not None:
        st.session_state[RUN_KEY] = run


# --------------------------------------------------------------------------- section 1: position file

def file_section(settings: Settings, creds: FyersCredentials, today: date, day: date) -> None:
    uploaded = st.file_uploader("Position file (.csv, or .xlsx — first sheet is read) with a Symbol column",
                                type=list(SUPPORTED_EXTENSIONS))
    if uploaded is None:
        st.caption("Every unique option in the file's Symbol column is fetched for the date above. "
                   "Expired contracts are listed as errors and not fetched.")
        return

    try:
        positions = read_position_file(uploaded.getvalue(), uploaded.name)
    except PositionFileError as exc:
        st.error(f"{uploaded.name}: {exc}")
        return
    if positions.counts.empty:
        st.error(f"{uploaded.name}: the Symbol column is empty.")
        return

    parsed = parse_all(positions.counts)
    configured = {i.name for i in settings.instruments}
    needed = sorted({p.underlying for p in parsed.values() if p and p.underlying in configured})
    masters: Dict[str, SymbolMaster] = {}
    try:
        for underlying in needed:
            masters[underlying] = master_for(settings, underlying, today)
    except SymbolMasterError as exc:
        st.error(f"Symbol master unavailable: {exc}")
        return
    resolution = resolve_symbols(positions.counts, parsed, masters, configured, today)

    # ---- what the file contains, before anything is fetched
    errors = [i for i in resolution.issues if i.status == STATUS_ERROR]
    skipped = [i for i in resolution.issues if i.status == STATUS_SKIPPED]
    st.markdown(f"**{uploaded.name}** · {indian_number(positions.file_rows)} rows · "
                f"{indian_number(len(positions.counts))} unique symbols · "
                f"**{len(resolution.contracts)} live contracts to fetch** · "
                f"{len(errors)} error(s) · {len(skipped)} skipped")
    if positions.blank_symbol_rows:
        st.caption(f"{indian_number(positions.blank_symbol_rows)} row(s) with a blank Symbol were ignored.")

    if resolution.contracts:
        overview = (pd.DataFrame([{"underlying": c.underlying, "expiry": c.expiry, "option_type": c.option_type}
                                  for c, _ in resolution.contracts])
                    .groupby(["underlying", "expiry", "option_type"]).size().unstack(fill_value=0)
                    .reset_index())
        overview["expiry"] = overview["expiry"].map(lambda d: f"{d:%d-%b-%Y}")
        st.dataframe(overview, hide_index=True)
    if errors:
        st.error(f"{len(errors)} contract(s) will NOT be fetched (expired or not listed) — see below "
                 f"and the summary sheet.")
    if resolution.issues:
        with st.expander(f"Not fetched: {len(errors)} error(s), {len(skipped)} skipped"):
            st.dataframe(pd.DataFrame([{"file_symbol": i.file_symbol, "rows": i.rows, "status": i.status,
                                        "message": i.message} for i in resolution.issues]),
                         hide_index=True, width="stretch")

    if not fetch_button(settings, len(resolution.contracts)):
        return

    fetched_underlyings = list(dict.fromkeys(c.underlying for c, _ in resolution.contracts))
    request = {"mode": MODE_FILE, "day": day,
               "title": f"**{uploaded.name}** · {len(resolution.contracts)} contract(s) · "
                        f"{', '.join(fetched_underlyings)}"}
    run = execute(settings, creds, resolution.contracts, day, resolution.issues, request,
                  sheets_by_underlying=True, stem=position_file_stem(uploaded.name, day))
    if run is not None:
        st.session_state[RUN_KEY] = run


# --------------------------------------------------------------------------- section 2: results

def _display_summary(summary: pd.DataFrame) -> pd.DataFrame:
    shown = summary.copy()
    shown["expiry"] = shown["expiry"].map(lambda d: "" if d is None or pd.isna(d) else f"{d:%d-%b-%Y}")
    shown["strike"] = shown["strike"].map(lambda s: "" if s is None or pd.isna(s) else strike_label(s))
    for column in ("first_candle", "last_candle"):
        shown[column] = shown[column].map(lambda t: "" if pd.isna(t) else f"{t:%H:%M}")
    shown["total_volume"] = shown["total_volume"].map(lambda v: "" if v is None or pd.isna(v)
                                                      else indian_number(v))
    return shown


def _data_table(frame: pd.DataFrame) -> None:
    if frame.empty:
        st.info("No candles came back for these contracts.")
        return
    st.caption(f"{indian_number(len(frame))} rows")
    st.dataframe(frame, hide_index=True, width="stretch", height=420, column_config={
        "expiry": st.column_config.DateColumn(format="DD-MMM-YYYY"),
        "datetime": st.column_config.DatetimeColumn(format="DD-MM-YYYY HH:mm"),
    })


def render_results(run: dict) -> None:
    st.subheader("2. Results")
    request = run["request"]
    st.markdown(f"{request['title']} · date **{request['day']:%d-%b-%Y}** · "
                f"fetched {run['fetched_at']:%H:%M:%S} IST")

    if run["auth_failed"]:
        st.error("Fyers rejected the access token — remaining contracts were skipped. "
                 "Refresh the token (DOR: `python fyer_code/fyers_auth.py`) and fetch again.")
    failed = sum(r.status == STATUS_ERROR for r in run["results"])
    not_fetched = sum(i.status == STATUS_ERROR for i in run["issues"])
    if failed or not_fetched:
        st.warning(f"{not_fetched} contract(s) not fetched and {failed} fetch error(s) — "
                   f"see the status column.")

    st.dataframe(_display_summary(run["summary"]), hide_index=True, width="stretch")
    for result in run["results"]:
        if result.warnings:
            st.warning(f"{result.contract.symbol}: " + "; ".join(result.warnings))

    sheets: Dict[str, pd.DataFrame] = run["sheets"]
    if len(sheets) == 1:
        _data_table(next(iter(sheets.values())))
    elif sheets:
        for tab, (name, frame) in zip(st.tabs(list(sheets)), sheets.items()):
            with tab:
                _data_table(frame)

    st.download_button("Download Excel", run["xlsx"], file_name=f"{run['stem']}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --------------------------------------------------------------------------- page

def main() -> None:
    st.set_page_config(page_title="Fyers option 1-min OHLC", layout="wide")
    st.title("Fyers option 1-min OHLC")

    try:
        settings = load_settings()
    except SettingsError as exc:
        st.error(f"Settings problem: {exc}")
        st.stop()
    configure_logging(settings.log_dir)
    today = now_ist().date()

    try:
        creds = load_credentials(settings.credentials_file)
    except CredentialsError as exc:
        st.error(str(exc))
        st.stop()
    expiry_text = f"valid until {creds.expires_at:%d-%m-%Y %H:%M} IST" if creds.expires_at else "expiry unknown"
    st.caption(f"Fyers token {expiry_text} · {settings.credentials_file}")

    st.subheader("1. Select contracts")
    col_mode, col_day, _ = st.columns([2, 1, 1])
    mode = col_mode.radio("Contracts from", [MODE_MANUAL, MODE_FILE], horizontal=True)
    day = col_day.date_input("Date", value=today, max_value=today, format="DD-MM-YYYY")
    if day.weekday() >= 5:
        st.warning(f"{day:%d-%b-%Y} is a {day:%A}; exchanges are normally closed, so expect no candles.")

    if mode == MODE_MANUAL:
        manual_section(settings, creds, today, day)
    else:
        file_section(settings, creds, today, day)

    run = st.session_state.get(RUN_KEY)
    if run is not None and (not isinstance(run, dict) or run.get("version") != RUN_VERSION):
        logger.info("discarding a result stored by an older version of the page")
        del st.session_state[RUN_KEY]
        st.info("The previous result was saved by an older version of this page and has been "
                "cleared — fetch again.")
        run = None
    if run is not None:
        render_results(run)


main()
