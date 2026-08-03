"""Startup validation/auto-correction of index instrument keys.

Upstox index instrument keys are exact strings from their instrument master
(e.g. "NSE_INDEX|Nifty 50", "NSE_INDEX|NIFTY MID SELECT") whose casing is
inconsistent between indices, so hand-written config keys are error-prone.
This module downloads the public NSE instrument master, checks each
configured key, and fixes wrong ones by fuzzy name match — disabling the
index (with suggestions) if no match exists.
"""

from __future__ import annotations

import logging
import re

from .config import IndexConfig
from .upstox_api import UpstoxAPI

log = logging.getLogger(__name__)

_ALIASES = {
    # common spelling differences between config names and NSE symbols
    "smallcap": "smlcap",
    "financialservices": "finservice",
    "midcapselect": "midselect",
    "niftynxt50": "niftynext50",
    "nxt50": "next50",
}


def _norm(text: str) -> str:
    out = re.sub(r"[^a-z0-9]", "", text.lower())
    for a, b in _ALIASES.items():
        out = out.replace(a, b)
    return out


def resolve_index_keys(instruments: list[IndexConfig], rows: list[dict] | None = None) -> None:
    """Validate/auto-correct instrument keys in place. `rows` overrides the
    downloaded instrument master (for tests)."""
    if rows is None:
        try:
            rows = UpstoxAPI.download_nse_instruments()
        except Exception as exc:  # noqa: BLE001 - resolution is best-effort
            log.warning("could not download instrument master to validate index keys: %s", exc)
            return

    index_rows = [r for r in rows if r.get("segment") == "NSE_INDEX" and r.get("instrument_key")]
    valid_keys = {r["instrument_key"] for r in index_rows}
    lookup: dict[str, str] = {}
    for r in index_rows:
        names = [r.get("trading_symbol"), r.get("name"), r["instrument_key"].split("|", 1)[-1]]
        for n in names:
            if n:
                lookup.setdefault(_norm(str(n)), r["instrument_key"])

    for ix in instruments:
        if not ix.enabled or ix.key in valid_keys:
            continue
        suffix = ix.key.split("|", 1)[-1]
        candidate = lookup.get(_norm(suffix)) or lookup.get(_norm(ix.name))
        if candidate:
            log.warning(
                "%s: instrument key %r not found in Upstox master; auto-corrected to %r "
                "(update config.yaml to silence this)", ix.name, ix.key, candidate,
            )
            ix.key = candidate
        else:
            needle = _norm(suffix)[:8] or _norm(ix.name)[:8]
            suggestions = sorted({k for nk, k in lookup.items() if needle and needle[:5] in nk})[:6]
            log.error(
                "%s: instrument key %r is invalid and could not be auto-resolved — "
                "disabling this index. Close matches: %s. Use "
                "`python -m nsemomentum instruments --index-only --search <text>` to find the key.",
                ix.name, ix.key, suggestions or "none",
            )
            ix.enabled = False
