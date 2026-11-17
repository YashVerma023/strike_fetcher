"""IST helpers. India has no daylight saving, so a fixed +05:30 offset is exact
and avoids depending on a timezone database being installed on Windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
IST_OFFSET_S = 5 * 3600 + 30 * 60


def now_ist() -> datetime:
    return datetime.now(IST)
