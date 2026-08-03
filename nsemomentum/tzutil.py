"""Timezone loading that works on Windows.

Windows Python ships no system tz database; ZoneInfo needs the `tzdata` pip
package there. If the key can't be resolved (tzdata missing), fall back to a
fixed offset — exact for Asia/Kolkata (UTC+05:30, no DST).
"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_FIXED_OFFSETS = {
    "Asia/Kolkata": timezone(timedelta(hours=5, minutes=30), "IST"),
    "Asia/Calcutta": timezone(timedelta(hours=5, minutes=30), "IST"),
}


def get_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        fallback = _FIXED_OFFSETS.get(name)
        if fallback is not None:
            log.warning(
                "IANA tz database not found (install the 'tzdata' package); "
                "using fixed offset %s for %s", fallback.utcoffset(None), name,
            )
            return fallback
        raise
