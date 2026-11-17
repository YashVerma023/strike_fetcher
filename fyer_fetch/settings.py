"""Load config/settings.json, with environment-variable overrides.

    FYER_FETCH_SETTINGS     path to a different settings file
    FYERS_CREDENTIALS_FILE  path to the Fyers credentials JSON (overrides
                            "credentials_file" in the settings file)

Relative paths in the settings file are resolved against the project root,
not the current working directory, so `streamlit run` works from anywhere.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.json"

ENV_SETTINGS_FILE = "FYER_FETCH_SETTINGS"
ENV_CREDENTIALS_FILE = "FYERS_CREDENTIALS_FILE"

# Symbol-master segments that carry option contracts.
VALID_SEGMENTS = ("NSE_FO", "BSE_FO", "MCX_COM")


class SettingsError(Exception):
    """The settings file is missing, unreadable or incomplete."""


@dataclass(frozen=True)
class SymbolMasterSettings:
    url_template: str
    cache_dir: Path
    cache_keep_files: int
    download_timeout_s: float
    download_retries: int
    retry_backoff_s: float


@dataclass(frozen=True)
class FyersSettings:
    min_call_interval_s: float
    max_retries: int
    rate_limit_backoff_s: float
    max_rate_limit_backoffs: int


@dataclass(frozen=True)
class Instrument:
    name: str      # underlying symbol exactly as the Fyers symbol master spells it
    segment: str   # NSE_FO | BSE_FO | MCX_COM


@dataclass(frozen=True)
class Settings:
    source_file: Path
    credentials_file: Path
    log_dir: Path
    symbol_master: SymbolMasterSettings
    fyers: FyersSettings
    instruments: Tuple[Instrument, ...]

    def instrument(self, name: str) -> Instrument:
        for item in self.instruments:
            if item.name == name:
                return item
        raise SettingsError(f"instrument {name!r} is not configured in {self.source_file.name}")

    def underlyings_in(self, segment: str) -> Tuple[str, ...]:
        return tuple(i.name for i in self.instruments if i.segment == segment)


def _resolve(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _require(section: Dict[str, Any], key: str, where: str) -> Any:
    if not isinstance(section, dict):
        raise SettingsError(f"{where} must be a JSON object")
    value = section.get(key)
    if value is None or value == "":
        raise SettingsError(f"{where}: '{key}' is missing")
    return value


def _positive(value: Any, key: str, where: str, cast=float, allow_zero: bool = False):
    try:
        number = cast(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{where}: '{key}' must be a number, got {value!r}") from exc
    if number < 0 or (number == 0 and not allow_zero):
        raise SettingsError(f"{where}: '{key}' must be {'>= 0' if allow_zero else '> 0'}, got {number}")
    return number


def _symbol_master(raw: Dict[str, Any], where: str) -> SymbolMasterSettings:
    where = f"{where} > symbol_master"
    url_template = str(_require(raw, "url_template", where)).strip()
    if "{segment}" not in url_template:
        raise SettingsError(f"{where}: 'url_template' must contain {{segment}}")
    return SymbolMasterSettings(
        url_template=url_template,
        cache_dir=_resolve(str(_require(raw, "cache_dir", where))),
        cache_keep_files=_positive(raw.get("cache_keep_files", 3), "cache_keep_files", where, int),
        download_timeout_s=_positive(raw.get("download_timeout_s", 60), "download_timeout_s", where),
        download_retries=_positive(raw.get("download_retries", 3), "download_retries", where, int),
        retry_backoff_s=_positive(raw.get("retry_backoff_s", 2), "retry_backoff_s", where, allow_zero=True),
    )


def _fyers(raw: Dict[str, Any], where: str) -> FyersSettings:
    where = f"{where} > fyers"
    if not isinstance(raw, dict):
        raise SettingsError(f"{where} must be a JSON object")
    return FyersSettings(
        min_call_interval_s=_positive(raw.get("min_call_interval_s", 1.3), "min_call_interval_s", where),
        max_retries=_positive(raw.get("max_retries", 5), "max_retries", where, int),
        rate_limit_backoff_s=_positive(raw.get("rate_limit_backoff_s", 60), "rate_limit_backoff_s", where),
        max_rate_limit_backoffs=_positive(raw.get("max_rate_limit_backoffs", 2),
                                          "max_rate_limit_backoffs", where, int, allow_zero=True),
    )


def _instruments(raw: Any, where: str) -> Tuple[Instrument, ...]:
    if not isinstance(raw, list) or not raw:
        raise SettingsError(f"{where}: 'instruments' must be a non-empty list")
    seen = set()
    out = []
    for position, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise SettingsError(f"{where}: instruments[{position}] must be an object")
        name = str(entry.get("name", "")).strip().upper()
        segment = str(entry.get("segment", "")).strip().upper()
        if not name:
            raise SettingsError(f"{where}: instruments[{position}] has no 'name'")
        if segment not in VALID_SEGMENTS:
            raise SettingsError(f"{where}: instrument {name} has segment {segment!r}; "
                                f"use one of {', '.join(VALID_SEGMENTS)}")
        if name in seen:
            raise SettingsError(f"{where}: instrument {name} is listed twice")
        seen.add(name)
        out.append(Instrument(name=name, segment=segment))
    return tuple(out)


def load_settings(path: Optional[Path] = None) -> Settings:
    """Read and validate the settings file. Raises SettingsError with a readable reason."""
    path = Path(path or os.environ.get(ENV_SETTINGS_FILE) or DEFAULT_SETTINGS_FILE)
    if not path.exists():
        raise SettingsError(f"settings file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SettingsError(f"{path} could not be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise SettingsError(f"{path} must contain a JSON object")

    where = path.name
    credentials_text = (os.environ.get(ENV_CREDENTIALS_FILE)
                        or str(_require(raw, "credentials_file", where)))
    settings = Settings(
        source_file=path,
        credentials_file=_resolve(credentials_text.strip()),
        log_dir=_resolve(str(raw.get("log_dir") or "logs")),
        symbol_master=_symbol_master(_require(raw, "symbol_master", where), where),
        fyers=_fyers(_require(raw, "fyers", where), where),
        instruments=_instruments(raw.get("instruments"), where),
    )
    logger.debug("settings loaded from %s", path)
    return settings
