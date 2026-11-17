"""Paced, retried calls to the Fyers Data API.

Pacing and retry rules are ported from the DOR project (marketdata._throttled):

* Standard plan Data API ceiling is 5 req/s, 50 req/min, and going over the
  per-minute cap more than 3 times in a day blocks the account for the rest of
  the day. Calls are therefore spaced `min_call_interval_s` apart (1.3s ~ 46/min).
* A genuine rate-limit response backs off a full minute (retrying inside the
  same minute window would just spend another of the day's strikes), at most
  `max_rate_limit_backoffs` times.
* Other errors get a short exponential retry, except ones that cannot succeed
  on retry (auth failure, invalid symbol), which return immediately.
* Pacing state and the lock are module-level: Streamlit serves every browser
  tab from ONE process, so two overlapping fetches must share one queue.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict

import requests

from .credentials import FyersCredentials
from .settings import FyersSettings

logger = logging.getLogger(__name__)

# Fyers has signalled "rate limit" as HTTP 429, code -429 and code -360; the
# message wording has been the stable part, so both are checked.
_RATE_LIMIT_CODES = {-360, -429, 429}
_RATE_LIMIT_WORDS = ("rate limit", "request limit", "requests/second", "requests/minute",
                     "requests/day", "upgrade to prime")

# Errors that will not change on retry. -16 "Could not authenticate the user"
# and -300 "Invalid symbol provided" are the codes seen in practice; the words
# catch the same conditions if Fyers renumbers them.
_AUTH_CODES = {-16}
_AUTH_WORDS = ("authenticate", "token expired", "invalid token", "token is invalid")
_PERMANENT_CODES = {-300, -50} | _AUTH_CODES
_PERMANENT_WORDS = ("invalid symbol", "valid symbol") + _AUTH_WORDS

_CALL_LOCK = threading.Lock()
_last_call_at = 0.0


class FyersClientError(Exception):
    """The Fyers SDK could not be imported or the client could not be created."""


def _message(resp: Dict[str, Any]) -> str:
    return str(resp.get("message") or "").lower()


def is_rate_limit(resp: Dict[str, Any]) -> bool:
    return resp.get("code") in _RATE_LIMIT_CODES or any(w in _message(resp) for w in _RATE_LIMIT_WORDS)


def is_auth_error(resp: Dict[str, Any]) -> bool:
    return resp.get("code") in _AUTH_CODES or any(w in _message(resp) for w in _AUTH_WORDS)


def is_permanent_error(resp: Dict[str, Any]) -> bool:
    return resp.get("code") in _PERMANENT_CODES or any(w in _message(resp) for w in _PERMANENT_WORDS)


class FyersDataClient:
    """Thin wrapper over fyersModel.FyersModel that paces and retries every call."""

    def __init__(self, credentials: FyersCredentials, settings: FyersSettings, sdk_log_dir: Path):
        try:
            from fyers_apiv3 import fyersModel
        except ImportError as exc:
            raise FyersClientError("fyers-apiv3 is not installed — run "
                                   "`pip install -r requirements.txt`") from exc
        sdk_log_dir.mkdir(parents=True, exist_ok=True)
        try:
            # log_path keeps the SDK's own fyersApi.log / fyersRequests.log out of the cwd
            self._sdk = fyersModel.FyersModel(client_id=credentials.client_id,
                                              token=credentials.access_token,
                                              is_async=False,
                                              log_path=str(sdk_log_dir),
                                              log_level="ERROR")
        except (TypeError, ValueError, OSError) as exc:
            raise FyersClientError(f"Fyers client could not be created ({exc})") from exc
        self._settings = settings

    def history(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("history", self._sdk.history, payload)

    def _call(self, name: str, func: Callable[..., Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        """Returns Fyers' own last response even when retries run out, so the
        caller sees the real code/message instead of an empty result."""
        global _last_call_at
        cfg = self._settings
        resp: Dict[str, Any] = {}
        rate_limit_backoffs = 0
        symbol = payload.get("symbol", "?")

        with _CALL_LOCK:
            for attempt in range(1, cfg.max_retries + 1):
                wait = cfg.min_call_interval_s - (time.monotonic() - _last_call_at)
                if wait > 0:
                    time.sleep(wait)
                try:
                    raw = func(data=payload)
                except (requests.exceptions.RequestException, ValueError) as exc:
                    # network failure or a non-JSON body; the SDK sets no timeout,
                    # so an unreachable host surfaces here after the OS gives up
                    raw = {"s": "error", "code": "network",
                           "message": f"{type(exc).__name__}: {exc}"}
                finally:
                    _last_call_at = time.monotonic()

                resp = raw if isinstance(raw, dict) else {
                    "s": "error", "code": "bad_response",
                    "message": f"unexpected response type {type(raw).__name__}"}

                if resp.get("s") != "error":
                    return resp

                if is_rate_limit(resp):
                    rate_limit_backoffs += 1
                    if rate_limit_backoffs > cfg.max_rate_limit_backoffs:
                        logger.error("%s %s: rate limit hit again after %d backoff(s) — stopping "
                                     "to avoid the daily account block (code %s: %s)", name, symbol,
                                     cfg.max_rate_limit_backoffs, resp.get("code"), resp.get("message"))
                        return resp
                    logger.warning("%s %s: rate limit (code %s: %s) — backing off %.0fs (%d/%d)",
                                   name, symbol, resp.get("code"), resp.get("message"),
                                   cfg.rate_limit_backoff_s, rate_limit_backoffs,
                                   cfg.max_rate_limit_backoffs)
                    time.sleep(cfg.rate_limit_backoff_s)
                    continue

                if is_permanent_error(resp):
                    logger.error("%s %s: not retryable (code %s: %s)", name, symbol,
                                 resp.get("code"), resp.get("message"))
                    return resp

                if attempt < cfg.max_retries:
                    delay = 2 ** (attempt - 1)
                    logger.warning("%s %s: error (code %s: %s) — retry %d/%d in %ss", name, symbol,
                                   resp.get("code"), resp.get("message"), attempt,
                                   cfg.max_retries - 1, delay)
                    time.sleep(delay)

            logger.error("%s %s: still failing after %d attempts (code %s: %s)", name, symbol,
                         cfg.max_retries, resp.get("code"), resp.get("message"))
            return resp
