"""Read and sanity-check the Fyers credentials file.

The file is the one the DOR project's `fyer_code/fyers_auth.py` writes each
morning (keys: client_id, secret_key, redirect_uri, access_token). Only
client_id and access_token are needed here. The token expires daily, so a stale
or wrong token is refused up front instead of failing every API call later.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .timeutil import IST, now_ist

logger = logging.getLogger(__name__)

REFRESH_HINT = ("run the DOR project's `python fyer_code/fyers_auth.py` to issue "
                "today's token (it writes the same credentials file)")


class CredentialsError(Exception):
    """The credentials file is missing, unreadable, or holds an unusable token."""


@dataclass(frozen=True)
class FyersCredentials:
    client_id: str
    access_token: str
    expires_at: Optional[datetime]   # IST; None when the token's expiry claim cannot be read


def token_claims(token: str) -> Dict[str, Any]:
    """The JWT payload, decoded WITHOUT signature verification (only Fyers can verify)."""
    try:
        body = str(token).split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
    except (IndexError, ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def load_credentials(path: Path, now: Optional[datetime] = None) -> FyersCredentials:
    now = now or now_ist()
    if not path.exists():
        raise CredentialsError(
            f"Fyers credentials file not found: {path}. Set 'credentials_file' in "
            f"config/settings.json or the FYERS_CREDENTIALS_FILE environment variable.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CredentialsError(f"{path} could not be read as JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise CredentialsError(f"{path} must contain a JSON object")

    client_id = str(data.get("client_id") or "").strip()
    access_token = str(data.get("access_token") or "").strip()
    missing = [k for k, v in (("client_id", client_id), ("access_token", access_token)) if not v]
    if missing:
        raise CredentialsError(f"{path.name} has no {', '.join(missing)} — {REFRESH_HINT}")

    claims = token_claims(access_token)
    # The browser redirect's auth_code is also a JWT and looks like a token, but
    # every API call rejects it. Its `sub` claim gives it away.
    if claims.get("sub") == "auth_code":
        raise CredentialsError(f"{path.name} holds the one-time AUTH CODE, not the access "
                               f"token — {REFRESH_HINT}")

    expires_at = None
    if "exp" in claims:
        try:
            expires_at = datetime.fromtimestamp(float(claims["exp"]), tz=IST)
        except (TypeError, ValueError, OverflowError, OSError):
            expires_at = None
    if expires_at is None:
        logger.warning("access token expiry could not be read; continuing — Fyers will "
                       "reject it if it is invalid")
    elif expires_at <= now:
        raise CredentialsError(f"Fyers access token expired at {expires_at:%d-%m-%Y %H:%M} IST "
                               f"— {REFRESH_HINT}")

    return FyersCredentials(client_id=client_id, access_token=access_token, expires_at=expires_at)
