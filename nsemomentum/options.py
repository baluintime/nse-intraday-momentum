"""ITM option selection per the strategy's risk framework.

Delta profile 0.65–0.75 (solidly ITM), nearest weekly/0DTE expiry. The
selection uses Upstox's option-chain endpoint, which returns per-strike
greeks, LTP, open interest and bid/ask. Strikes that fail the configured
liquidity guards (min open interest, max bid-ask spread) are skipped so thin
strikes with wide spreads aren't traded. If greeks are missing, falls back to
a strike roughly two steps in the money among the liquid strikes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from .config import Config
from .upstox_api import UpstoxAPI

log = logging.getLogger(__name__)


@dataclass
class OptionSelection:
    instrument_key: str
    trading_symbol: str
    strike: float
    option_type: str  # "CE" | "PE"
    expiry: str  # YYYY-MM-DD
    lot_size: int
    ltp: float | None
    delta: float | None
    oi: float | None = None
    volume: float | None = None
    spread_pct: float | None = None


class OptionSelector:
    def __init__(self, api: UpstoxAPI, cfg: Config):
        self.api = api
        self.cfg = cfg
        self._contracts_cache: dict[str, list[dict]] = {}

    # ------------------------------------------------------------- expiries

    def _contracts(self, underlying_key: str) -> list[dict]:
        if underlying_key not in self._contracts_cache:
            try:
                self._contracts_cache[underlying_key] = self.api.option_contracts(underlying_key)
            except Exception as exc:  # noqa: BLE001 - report and treat as no contracts
                log.warning("option contracts lookup failed for %s: %s", underlying_key, exc)
                self._contracts_cache[underlying_key] = []
        return self._contracts_cache[underlying_key]

    def has_options(self, underlying_key: str) -> bool:
        return bool(self._contracts(underlying_key))

    def nearest_expiry(self, underlying_key: str, today: date | None = None) -> str | None:
        """Nearest unexpired expiry, skipping any within `min_days_to_expiry`
        days (too close to expiry — high gamma/theta/pin risk) and rolling to
        the next. Falls back to the farthest available if all are within it."""
        today = today or datetime.now().date()
        expiries = set()
        for c in self._contracts(underlying_key):
            exp = c.get("expiry")
            if exp:
                expiries.add(str(exp)[:10])
        future = sorted(e for e in expiries if date.fromisoformat(e) >= today)
        if not future:
            return None
        min_days = self.cfg.min_days_to_expiry
        for e in future:
            if (date.fromisoformat(e) - today).days > min_days:
                return e
        # every listed expiry is within the threshold — use the farthest one
        log.warning(
            "%s: all expiries are within %d day(s); using the farthest (%s)",
            underlying_key, min_days, future[-1],
        )
        return future[-1]

    def lot_size(self, underlying_key: str, expiry: str) -> int:
        for c in self._contracts(underlying_key):
            if str(c.get("expiry", ""))[:10] == expiry and c.get("lot_size"):
                return int(c["lot_size"])
        for c in self._contracts(underlying_key):
            if c.get("lot_size"):
                return int(c["lot_size"])
        return 1

    # ------------------------------------------------------------ liquidity

    @staticmethod
    def _num(value) -> float | None:
        """Parse a numeric field, keeping 0 (present-but-zero) distinct from None (absent)."""
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _liquidity(self, md: dict) -> tuple[float | None, float | None, float | None, float | None, float | None]:
        """Returns (oi, volume, bid, ask, spread_pct). spread_pct only when a two-sided quote exists."""
        oi = self._num(md.get("oi"))
        volume = self._num(md.get("volume"))
        bid = self._num(md.get("bid_price"))
        ask = self._num(md.get("ask_price"))
        spread_pct = None
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid * 100.0 if mid else None
        return oi, volume, bid, ask, spread_pct

    def _is_liquid(
        self,
        oi: float | None,
        volume: float | None,
        bid: float | None,
        ask: float | None,
        spread_pct: float | None,
    ) -> tuple[bool, str]:
        """Apply the configured liquidity guards. Missing data (None) is treated as
        'unknown' and passes, so trading isn't blocked when the feed omits a field;
        an explicit zero (a present-but-dead value) fails. The volume guard is key:
        an untraded strike has a stale LTP/greeks that mislead delta selection and
        fabricate PnL, so it must never be chosen."""
        if self.cfg.min_volume > 0 and volume is not None and volume < self.cfg.min_volume:
            return False, f"volume {volume:.0f} < {self.cfg.min_volume}"
        if self.cfg.min_open_interest > 0 and oi is not None and oi < self.cfg.min_open_interest:
            return False, f"OI {oi:.0f} < {self.cfg.min_open_interest}"
        if self.cfg.max_spread_pct > 0 and bid is not None and ask is not None:
            if bid <= 0 or ask <= 0:
                return False, "no two-sided quote"
            if spread_pct is not None and spread_pct > self.cfg.max_spread_pct:
                return False, f"spread {spread_pct:.1f}% > {self.cfg.max_spread_pct:.1f}%"
        return True, ""

    # ------------------------------------------------------------ selection

    def select_itm(self, underlying_key: str, direction: str, spot: float) -> OptionSelection | None:
        """Pick an ITM option for `direction` ("LONG" -> CE, "SHORT" -> PE).

        Considers only strikes that pass the liquidity guards, then prefers the
        one whose |delta| is closest to target_delta within [delta_min,
        delta_max]; falls back to ~2 strikes in the money if greeks are missing.
        Returns None (skip the trade) if no liquid ITM strike exists.
        """
        expiry = self.nearest_expiry(underlying_key)
        if not expiry:
            return None
        opt_field = "call_options" if direction == "LONG" else "put_options"
        opt_type = "CE" if direction == "LONG" else "PE"
        try:
            chain = self.api.option_chain(underlying_key, expiry)
        except Exception as exc:  # noqa: BLE001
            log.warning("option chain fetch failed for %s %s: %s", underlying_key, expiry, exc)
            return None
        if not chain:
            return None

        lot = self.lot_size(underlying_key, expiry)
        # each entry: (strike, leg, ltp, delta, oi, volume, spread_pct)
        candidates: list[tuple[float, tuple]] = []  # (delta_score, entry)
        fallback: list[tuple] = []
        illiquid_skipped = 0

        for row in chain:
            strike = float(row.get("strike_price", 0) or 0)
            leg = row.get(opt_field) or {}
            if not leg.get("instrument_key"):
                continue
            itm = strike < spot if opt_type == "CE" else strike > spot
            if not itm:
                continue
            md = leg.get("market_data") or {}
            ltp = self._num(md.get("ltp"))
            ltp = ltp if ltp not in (None, 0) else None
            delta = self._num((leg.get("option_greeks") or {}).get("delta"))
            oi, volume, bid, ask, spread_pct = self._liquidity(md)
            liquid, reason = self._is_liquid(oi, volume, bid, ask, spread_pct)
            if not liquid:
                illiquid_skipped += 1
                log.debug("%s %s strike %.0f skipped (%s)", underlying_key, opt_type, strike, reason)
                continue
            entry = (strike, leg, ltp, delta, oi, volume, spread_pct)
            fallback.append(entry)
            if delta is not None and self.cfg.delta_min <= abs(delta) <= self.cfg.delta_max:
                candidates.append((abs(abs(delta) - self.cfg.target_delta), entry))

        if candidates:
            candidates.sort(key=lambda t: t[0])
            strike, leg, ltp, delta, oi, volume, spread_pct = candidates[0][1]
        elif fallback:
            # ~2 strikes in the money among the *liquid* strikes: for calls the
            # 2nd-highest strike below spot; for puts the 2nd-lowest above spot.
            fallback.sort(key=lambda t: t[0], reverse=(opt_type == "CE"))
            strike, leg, ltp, delta, oi, volume, spread_pct = fallback[min(1, len(fallback) - 1)]
            log.warning(
                "%s %s: no strike with delta in [%.2f, %.2f]; fell back to strike %.0f",
                underlying_key, expiry, self.cfg.delta_min, self.cfg.delta_max, strike,
            )
        else:
            log.warning(
                "%s %s: no liquid ITM strike found (skipped %d thin strike(s)); entry skipped",
                underlying_key, opt_type, illiquid_skipped,
            )
            return None

        return OptionSelection(
            instrument_key=leg["instrument_key"],
            trading_symbol=leg.get("trading_symbol") or leg.get("tradingsymbol") or leg["instrument_key"],
            strike=strike,
            option_type=opt_type,
            expiry=expiry,
            lot_size=lot,
            ltp=ltp,
            delta=delta,
            oi=oi,
            volume=volume,
            spread_pct=spread_pct,
        )
