"""Build the NSE F&O stock universe from the Upstox instrument master.

The momentum scanner ranks the *whole* NSE F&O stock universe, so it needs the
list of underlyings that have stock derivatives, each mapped to its equity
instrument key (for price/gap/RVOL) and its near-month futures key (for the ΔOI
build-up gate). Index derivatives (NIFTY/BANKNIFTY futures) are excluded — this
is a stock-options strategy.

Upstox's per-exchange instrument dump (``NSE.json.gz``) lists every NSE
instrument; each futures row carries ``underlying_key`` (the equity key) and an
``expiry``, which is all we need to pick the near-month contract per underlying.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from .config import MomentumSymbol
from .upstox_api import UpstoxAPI

log = logging.getLogger(__name__)


def _parse_expiry(value) -> date | None:
    """Upstox expiries come as epoch-millis (int) or an ISO string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def build_fno_universe(
    rows: list[dict] | None = None, today: date | None = None
) -> list[MomentumSymbol]:
    """All NSE F&O stock underlyings with a live near-month futures contract.

    Returns a name-sorted list of :class:`MomentumSymbol` (equity key + near-month
    futures key). ``rows`` overrides the downloaded instrument master (for tests).
    """
    today = today or datetime.now().date()
    if rows is None:
        rows = UpstoxAPI.download_nse_instruments()

    # underlying_key -> (near expiry, name, futures_key)
    best: dict[str, tuple[date, str, str]] = {}
    for r in rows:
        if r.get("segment") != "NSE_FO":
            continue
        if str(r.get("instrument_type", "")).upper() != "FUT":
            continue
        under = r.get("underlying_key") or ""
        # stock futures only — the underlying is an equity, not an index
        if not under.startswith("NSE_EQ|"):
            continue
        fkey = r.get("instrument_key")
        if not fkey:
            continue
        exp = _parse_expiry(r.get("expiry"))
        if exp is None or exp < today:
            continue
        name = (
            r.get("underlying_symbol")
            or r.get("asset_symbol")
            or r.get("name")
            or under.split("|", 1)[-1]
        )
        cur = best.get(under)
        if cur is None or exp < cur[0]:
            best[under] = (exp, str(name), fkey)

    universe = [
        MomentumSymbol(name=name, key=under, futures_key=fkey)
        for under, (_exp, name, fkey) in best.items()
    ]
    universe.sort(key=lambda s: s.name)
    log.info("built NSE F&O universe: %d stock underlyings", len(universe))
    return universe
