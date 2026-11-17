"""One logging configuration per process (Streamlit re-runs the page script on
every interaction, but imported modules stay loaded, so a module flag is enough
to stop handlers being added again on each rerun)."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    """Log to logs/fyer_fetch_<date>.log and the console. Returns the log file path."""
    global _CONFIGURED
    log_file = log_dir / f"fyer_fetch_{date.today():%Y-%m-%d}.log"
    if _CONFIGURED:
        return log_file

    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_FORMAT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    package_logger = logging.getLogger("fyer_fetch")
    package_logger.setLevel(level)
    package_logger.addHandler(file_handler)
    package_logger.addHandler(console_handler)
    package_logger.propagate = False

    _CONFIGURED = True
    package_logger.info("logging to %s", log_file)
    return log_file
