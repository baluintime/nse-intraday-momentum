"""Execution layer: shared position/trade bookkeeping with a simulated
(paper) implementation and a real Upstox (live) implementation.

Both brokers hold at most one position per pipeline and log every fill to a
CSV trade log. In both directions the strategy only ever BUYS options
(calls for longs, puts for shorts), so "exit" always means selling what we
hold. Only real exchange-listed contracts are traded — indices with no
options are signal-only and never reach the broker.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time as _time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

from .config import Config
from .upstox_api import UpstoxAPI, UpstoxError

log = logging.getLogger(__name__)

TICK = 0.05


def _round_tick(price: float) -> float:
    return max(TICK, round(round(price / TICK) * TICK, 2))


@dataclass
class Position:
    pipeline_id: str
    instrument_key: str
    symbol: str
    qty: int
    entry_price: float
    entry_time: str
    direction: str  # "LONG" | "SHORT" (underlying view; the option is always bought)
    entry_spot: float | None = None  # underlying index level at entry
    strike: float | None = None  # option strike price
    lot_size: int | None = None  # exchange lot size (for whole-lot partial exits)
    partial_taken: bool = False  # a partial profit exit has already fired

    def pnl(self, exit_price: float) -> float:
        return (exit_price - self.entry_price) * self.qty


@dataclass
class BrokerState:
    cash: float = 0.0
    realized_pnl_today: float = 0.0
    pnl_date: str = ""
    positions: dict[str, Position] = field(default_factory=dict)


class BaseBroker:
    """Common bookkeeping; subclasses implement _fill_buy/_fill_sell."""

    def __init__(self, cfg: Config, trade_log_path: str):
        self.cfg = cfg
        self.state = BrokerState(pnl_date=datetime.now().strftime("%Y-%m-%d"))
        self.trade_log_path = trade_log_path
        self.api = None  # set by subclasses; used for live mark-to-market
        os.makedirs(os.path.dirname(trade_log_path) or ".", exist_ok=True)

    # -- interface -----------------------------------------------------

    def position(self, pipeline_id: str) -> Position | None:
        return self.state.positions.get(pipeline_id)

    def position_side(self, pipeline_id: str) -> str | None:
        pos = self.position(pipeline_id)
        return pos.direction if pos else None

    def open_positions(self) -> list[Position]:
        return list(self.state.positions.values())

    def enter(
        self,
        pipeline_id: str,
        instrument_key: str,
        symbol: str,
        qty: int,
        direction: str,
        price_hint: float | None,
        underlying_spot: float | None = None,
        strike: float | None = None,
        lot_size: int | None = None,
    ) -> Position | None:
        if pipeline_id in self.state.positions:
            log.warning("%s already holds a position; entry skipped", pipeline_id)
            return None
        fill = self._fill_buy(pipeline_id, instrument_key, qty, price_hint)
        if fill is None:
            return None
        pos = Position(
            pipeline_id=pipeline_id,
            instrument_key=instrument_key,
            symbol=symbol,
            qty=qty,
            entry_price=fill,
            entry_time=datetime.now().isoformat(timespec="seconds"),
            direction=direction,
            entry_spot=underlying_spot,
            strike=strike,
            lot_size=lot_size,
        )
        self.state.positions[pipeline_id] = pos
        self._log_trade("ENTRY", pos, fill, 0.0, index_price=underlying_spot)
        log.info(
            "%s entered %s @ %.2f (index %s)", pipeline_id, pos.symbol, fill,
            f"{underlying_spot:.2f}" if underlying_spot is not None else "n/a",
        )
        self._persist()
        return pos

    def exit(
        self,
        pipeline_id: str,
        price_hint: float | None,
        note: str = "",
        underlying_spot: float | None = None,
    ) -> float | None:
        pos = self.state.positions.get(pipeline_id)
        if pos is None:
            return None
        fill = self._fill_sell(pos, price_hint)
        if fill is None:
            return None
        pnl = pos.pnl(fill)
        # sanity guard: a non-positive entry price means the entry was never
        # priced (e.g. an adopted position with a missing avg) — its PnL is
        # meaningless, so record 0 rather than fabricate a huge number.
        if pos.entry_price is None or pos.entry_price <= 0:
            log.warning(
                "%s exit: entry price is %s — PnL untrustworthy, recording 0 (fill %.2f x%d)",
                pipeline_id, pos.entry_price, fill, pos.qty,
            )
            pnl = 0.0
        self._roll_pnl_date()
        self.state.realized_pnl_today += pnl
        del self.state.positions[pipeline_id]
        self._log_trade(f"EXIT{(' ' + note) if note else ''}", pos, fill, pnl, index_price=underlying_spot)
        self._persist()
        log.info(
            "%s exited %s @ %.2f pnl=%+.2f (index %s vs entry %s)",
            pipeline_id, pos.symbol, fill, pnl,
            f"{underlying_spot:.2f}" if underlying_spot is not None else "n/a",
            f"{pos.entry_spot:.2f}" if pos.entry_spot is not None else "n/a",
        )
        return pnl

    def partial_exit(self, pipeline_id: str, fraction: float, price_hint: float | None = None) -> float | None:
        """Close a whole-lot portion (~`fraction`) of a position once, keeping the
        rest. No-op if it can't be split into lots (single lot) or already done."""
        pos = self.state.positions.get(pipeline_id)
        if pos is None or pos.partial_taken or not pos.lot_size or fraction <= 0 or fraction >= 1:
            return None
        lots = pos.qty // pos.lot_size
        if lots < 2:  # can't sell a fraction of a single lot
            return None
        sell_lots = max(1, min(lots - 1, int(round(lots * fraction))))
        sell_qty = sell_lots * pos.lot_size
        tmp = Position(
            pipeline_id=pipeline_id, instrument_key=pos.instrument_key, symbol=pos.symbol,
            qty=sell_qty, entry_price=pos.entry_price,
            entry_time=datetime.now().isoformat(timespec="seconds"), direction=pos.direction,
            strike=pos.strike, lot_size=pos.lot_size,
        )
        fill = self._fill_sell(tmp, price_hint)
        if fill is None:
            return None
        pnl = 0.0 if (pos.entry_price is None or pos.entry_price <= 0) else (fill - pos.entry_price) * sell_qty
        self._roll_pnl_date()
        self.state.realized_pnl_today += pnl
        pos.qty -= sell_qty
        pos.partial_taken = True
        self._log_trade("PARTIAL EXIT", tmp, fill, pnl)
        self._persist()
        log.info("%s partial exit %s x%d @ %.2f pnl=%+.2f (%d lot(s) left)",
                 pipeline_id, pos.symbol, sell_qty, fill, pnl, pos.qty // pos.lot_size)
        return pnl

    def realized_pnl_today(self) -> float:
        self._roll_pnl_date()
        return self.state.realized_pnl_today

    def mark_to_market(self) -> tuple[float, dict[str, dict]]:
        """Current unrealized PnL across open positions, plus per-position detail
        (ltp, upnl, strike, ...). Uses the broker's API for live option LTPs."""
        positions = self.open_positions()
        detail: dict[str, dict] = {}
        total = 0.0
        ltps: dict[str, float] = {}
        if positions and self.api is not None:
            try:
                ltps = self.api.ltp([p.instrument_key for p in positions])
            except Exception as exc:  # noqa: BLE001 - MTM is best-effort
                log.debug("mark-to-market LTP fetch failed: %s", exc)
        for p in positions:
            ltp = ltps.get(p.instrument_key)
            upnl = p.pnl(ltp) if ltp is not None else None
            if upnl is not None:
                total += upnl
            detail[p.pipeline_id] = {
                "symbol": p.symbol, "strike": p.strike, "direction": p.direction,
                "qty": p.qty, "entry_price": p.entry_price, "ltp": ltp, "upnl": upnl,
                "entry_spot": p.entry_spot,
            }
        return total, detail

    def total_pnl(self) -> float:
        """Realized today + current unrealized (mark-to-market)."""
        return self.realized_pnl_today() + self.mark_to_market()[0]

    # -- broker reconciliation (live only; paper is its own source of truth) --

    def reconcile(self) -> list[dict]:
        """Return per-instrument mismatches between the app book and the real
        broker. Empty = in sync. Paper mode is always in sync."""
        return []

    def recently_ordered(self, instrument_key: str, within: float = 20.0) -> bool:
        """Whether we placed an order on this instrument very recently (live only)."""
        return False

    def seed_from_upstox(self, keep=None) -> int:
        """Adopt any untracked real positions into the app book so square-off
        closes what actually exists. No-op for paper. Returns count adopted.
        `keep(symbol)`, when given, restricts adoption to positions it accepts."""
        return 0

    def adopt_position(
        self, pipeline_id: str, instrument_key: str, symbol: str, qty: int,
        entry_price: float, direction: str, strike: float | None = None,
    ) -> Position:
        """Attach an untracked (broker-side) position to a pipeline so the
        strategy manages its exit. Overwrites any existing entry for the id."""
        pos = Position(
            pipeline_id=pipeline_id, instrument_key=instrument_key, symbol=symbol, qty=qty,
            entry_price=entry_price, entry_time=datetime.now().isoformat(timespec="seconds"),
            direction=direction, strike=strike,
        )
        self.state.positions[pipeline_id] = pos
        self._persist()
        log.warning("reconcile: adopted %s x%d @ %.2f into %s (%s) — strategy will manage it",
                    symbol, qty, entry_price, pipeline_id, direction)
        return pos

    def drop_position(self, pipeline_id: str, reason: str = "closed externally") -> None:
        """Remove a phantom position the broker no longer holds (closed outside
        the app), logging it so the record shows why."""
        pos = self.state.positions.pop(pipeline_id, None)
        if pos is not None:
            self._log_trade(f"EXIT {reason}", pos, pos.entry_price, 0.0, index_price=pos.entry_spot)
            self._persist()
            log.warning("reconcile: dropped phantom %s (%s) — %s", pos.symbol, pipeline_id, reason)

    def square_off_instrument(
        self, instrument_key: str, symbol: str, qty: int, price_hint: float | None = None
    ) -> float | None:
        """Close an untracked broker position that no pipeline can manage. Uses a
        transient position (never added to the book) so a failed sell leaves the
        book unchanged (the orphan stays flagged rather than becoming a phantom)."""
        log.warning("reconcile: no pipeline can manage %s x%d — squaring it off", symbol, qty)
        tmp = Position(
            pipeline_id=f"ORPHAN:{symbol}", instrument_key=instrument_key, symbol=symbol, qty=qty,
            entry_price=price_hint or 0.0, entry_time=datetime.now().isoformat(timespec="seconds"),
            direction="LONG",
        )
        try:
            fill = self._fill_sell(tmp, price_hint)
        except Exception as exc:  # noqa: BLE001
            log.error("reconcile: square-off of %s failed: %s", symbol, exc)
            return None
        if fill is None:
            log.error("reconcile: square-off of %s did not fill; still untracked", symbol)
            return None
        self._log_trade("EXIT reconcile:orphan", tmp, fill, 0.0)
        self._persist()
        log.warning("reconcile: squared off untracked %s x%d @ %.2f", symbol, qty, fill)
        return fill

    # -- hooks ----------------------------------------------------------

    def _fill_buy(
        self, pipeline_id: str, instrument_key: str, qty: int, price_hint: float | None
    ) -> float | None:
        raise NotImplementedError

    def _fill_sell(self, pos: Position, price_hint: float | None) -> float | None:
        raise NotImplementedError

    def _persist(self) -> None:
        pass

    # -- helpers ---------------------------------------------------------

    def _roll_pnl_date(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.pnl_date != today:
            self.state.pnl_date = today
            self.state.realized_pnl_today = 0.0

    def _log_trade(
        self, action: str, pos: Position, price: float, pnl: float, index_price: float | None = None
    ) -> None:
        new_file = not os.path.exists(self.trade_log_path)
        with open(self.trade_log_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(
                    ["time", "pipeline", "action", "symbol", "instrument_key",
                     "direction", "qty", "price", "index_price", "pnl"]
                )
            writer.writerow(
                [datetime.now().isoformat(timespec="seconds"), pos.pipeline_id, action,
                 pos.symbol, pos.instrument_key, pos.direction, pos.qty,
                 f"{price:.2f}", f"{index_price:.2f}" if index_price is not None else "",
                 f"{pnl:.2f}"]
            )


class PaperBroker(BaseBroker):
    """Simulated fills at live LTP with configurable slippage; state persists
    to JSON so a restart resumes open paper positions."""

    def __init__(self, cfg: Config, api: UpstoxAPI | None):
        super().__init__(cfg, cfg.paper_trade_log)
        self.api = api
        self.state.cash = cfg.paper_starting_cash
        self._state_file = cfg.paper_state_file
        self._load()

    def _quote(self, instrument_key: str, price_hint: float | None) -> float | None:
        if self.api is not None:
            try:
                ltp = self.api.ltp_single(instrument_key)
                if ltp:
                    return ltp
            except UpstoxError as exc:
                log.warning("LTP fetch failed for %s: %s", instrument_key, exc)
        return price_hint

    def _fill_buy(self, pipeline_id, instrument_key, qty, price_hint):
        price = self._quote(instrument_key, price_hint)
        if price is None:
            log.error("paper buy skipped: no price for %s", instrument_key)
            return None
        fill = _round_tick(price * (1 + self.cfg.paper_slippage_pct / 100.0))
        cost = fill * qty
        if cost > self.state.cash:
            log.error("paper buy skipped: cost %.2f exceeds cash %.2f", cost, self.state.cash)
            return None
        self.state.cash -= cost
        return fill

    def _fill_sell(self, pos, price_hint):
        price = self._quote(pos.instrument_key, price_hint)
        if price is None:
            log.error("paper sell has no live price for %s; using entry price", pos.instrument_key)
            price = pos.entry_price
        fill = _round_tick(price * (1 - self.cfg.paper_slippage_pct / 100.0))
        self.state.cash += fill * pos.qty
        return fill

    # -- persistence ------------------------------------------------------

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self._state_file) or ".", exist_ok=True)
        payload = {
            "cash": self.state.cash,
            "realized_pnl_today": self.state.realized_pnl_today,
            "pnl_date": self.state.pnl_date,
            "positions": {k: asdict(v) for k, v in self.state.positions.items()},
        }
        with open(self._state_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def _load(self) -> None:
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file, encoding="utf-8") as fh:
                payload = json.load(fh)
            self.state.cash = float(payload.get("cash", self.state.cash))
            self.state.realized_pnl_today = float(payload.get("realized_pnl_today", 0.0))
            self.state.pnl_date = payload.get("pnl_date", self.state.pnl_date)
            known = {f.name for f in fields(Position)}
            self.state.positions = {
                k: Position(**{a: b for a, b in v.items() if a in known})
                for k, v in (payload.get("positions") or {}).items()
            }
            if self.state.positions:
                log.info("resumed %d open paper position(s)", len(self.state.positions))
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("could not load paper state (%s); starting fresh", exc)


class LiveBroker(BaseBroker):
    """Places real orders on Upstox. Uses marketable LIMIT orders (LTP +/-
    limit_tolerance_pct) per the spec's slippage guidance, or MARKET if
    configured. Fill price is read back from order details."""

    FILL_POLL_SECONDS = 15
    MARKET_CONFIRM_SECONDS = 6  # how long to poll for the market retry to confirm before giving up

    # Upstox order statuses that mean the order is done (no longer working)
    _TERMINAL = {"complete", "rejected", "cancelled", "cancelled after market order"}

    def __init__(self, cfg: Config, api: UpstoxAPI):
        super().__init__(cfg, cfg.live_trade_log)
        self.api = api
        self.pending_unconfirmed = False  # a fill we couldn't confirm — force a reconcile
        # per-position in-flight order guard: pipeline_id -> last order_id not yet
        # confirmed terminal. Prevents firing a duplicate order while one is working.
        self._pending: dict[str, str] = {}
        # instrument_key -> ts of the last order we placed on it. Lets reconcile
        # avoid squaring off an instrument we just traded before Upstox reflects it.
        self._recent_orders: dict[str, float] = {}

    def recently_ordered(self, instrument_key: str, within: float = 20.0) -> bool:
        ts = self._recent_orders.get(instrument_key)
        return ts is not None and (_time.time() - ts) < within

    # -- reconciliation against the real Upstox position book -----------

    def _upstox_net(self) -> dict[str, dict]:
        """Net (non-zero) Upstox positions keyed by instrument, {qty, avg, symbol}."""
        out: dict[str, dict] = {}
        for p in self.api.positions():
            key = p.get("instrument_token") or p.get("instrument_key")
            qty = int(p.get("quantity") or 0)
            if not key or qty == 0:
                continue
            out[key] = {
                "qty": qty,
                "avg": float(p.get("average_price") or 0.0),
                "symbol": p.get("tradingsymbol") or p.get("trading_symbol") or key,
            }
        return out

    def _app_net(self) -> dict[str, int]:
        net: dict[str, int] = {}
        for pos in self.open_positions():
            net[pos.instrument_key] = net.get(pos.instrument_key, 0) + pos.qty
        return net

    def reconcile(self) -> list[dict]:
        try:
            ups = self._upstox_net()
        except UpstoxError as exc:
            log.warning("reconcile: could not fetch Upstox positions: %s", exc)
            return []  # can't compare — don't raise a false mismatch
        app = self._app_net()
        mismatches = []
        for key in set(ups) | set(app):
            u = ups.get(key, {}).get("qty", 0)
            a = app.get(key, 0)
            if u != a:
                mismatches.append({
                    "instrument": key,
                    "symbol": ups.get(key, {}).get("symbol", key),
                    "app_qty": a,
                    "upstox_qty": u,
                    "avg": ups.get(key, {}).get("avg", 0.0),
                })
        return mismatches

    def seed_from_upstox(self, keep=None) -> int:
        try:
            ups = self._upstox_net()
        except UpstoxError as exc:
            log.warning("seed: could not fetch Upstox positions: %s", exc)
            return 0
        app = self._app_net()
        added = 0
        for key, info in ups.items():
            delta = info["qty"] - app.get(key, 0)
            if delta <= 0:  # already tracked (or app thinks it holds more — reconcile flags that)
                continue
            if keep is not None and not keep(info["symbol"]):
                log.info("seed: %s not in the current picks — not adopting", info["symbol"])
                continue
            pid = base = f"ADOPTED:{info['symbol']}"
            i = 1
            while pid in self.state.positions:
                i += 1
                pid = f"{base}#{i}"
            # a bought PUT is a SHORT (bearish) position in our book; a CE is LONG.
            direction = "SHORT" if str(info["symbol"]).strip().upper().endswith("PE") else "LONG"
            self.state.positions[pid] = Position(
                pipeline_id=pid, instrument_key=key, symbol=info["symbol"], qty=delta,
                entry_price=info["avg"], entry_time=datetime.now().isoformat(timespec="seconds"),
                direction=direction,
            )
            added += 1
            log.warning("adopted untracked Upstox position %s x%d @ %.2f", info["symbol"], delta, info["avg"])
        return added

    def _resolve_pending(self, key: str, side: str) -> tuple[str, float | None]:
        """Resolve any in-flight order for `key`. Returns (state, fill):
        state = 'none' (nothing pending), 'working' (still live — do NOT duplicate),
        'filled' (it already filled — fill price returned), or 'clear' (terminal
        non-fill — safe to place a new order)."""
        oid = self._pending.get(key)
        if not oid:
            return "none", None
        try:
            d = self.api.order_details(oid)
        except UpstoxError:
            # can't tell — assume still working so we never place a duplicate blindly
            log.warning("%s: could not check pending order %s; assuming still working", key, oid)
            return "working", None
        status = (d.get("status") or "").lower()
        if status == "complete":
            self._pending.pop(key, None)
            avg = d.get("average_price")
            log.warning(
                "%s: prior %s order %s had already FILLED @ %s — using it, not placing a duplicate",
                key, side, oid, avg,
            )
            return "filled", (float(avg) if avg else None)
        if status in self._TERMINAL:
            self._pending.pop(key, None)
            return "clear", None
        log.warning(
            "%s: an order (%s, status=%s) is still working — NOT placing a duplicate %s",
            key, oid, status, side,
        )
        return "working", None

    def _place_and_wait(
        self, key: str, instrument_key: str, qty: int, side: str, ltp: float | None
    ) -> float | None:
        # in-flight guard: never stack a second order on a position whose prior
        # order isn't confirmed terminal (this is what caused the double-sell)
        state, fill = self._resolve_pending(key, side)
        if state == "working":
            return None
        if state == "filled":
            return fill if fill is not None else (ltp or 0.0)

        order_type = self.cfg.order_type if ltp else "MARKET"
        price = 0.0
        if order_type == "LIMIT" and ltp:
            tol = self.cfg.limit_tolerance_pct / 100.0
            price = _round_tick(ltp * (1 + tol) if side == "BUY" else ltp * (1 - tol))
        try:
            order_id = self.api.place_order(
                instrument_key=instrument_key,
                quantity=qty,
                transaction_type=side,
                order_type=order_type,
                price=price,
                product=self.cfg.product,
            )
        except UpstoxError as exc:
            log.error("order placement failed (%s %s x%d): %s", side, instrument_key, qty, exc)
            return None
        self._pending[key] = order_id  # now in flight — guards against a duplicate
        self._recent_orders[instrument_key] = _time.time()
        log.info("placed %s %s x%d %s@%.2f order_id=%s", side, instrument_key, qty, order_type, price, order_id)

        deadline = _time.time() + self.FILL_POLL_SECONDS
        status = ""
        while _time.time() < deadline:
            try:
                details = self.api.order_details(order_id)
            except UpstoxError:
                _time.sleep(1)
                continue
            status = (details.get("status") or "").lower()
            if status == "complete":
                self._pending.pop(key, None)
                avg = details.get("average_price")
                return float(avg) if avg else (price or ltp or 0.0)
            if status in ("rejected", "cancelled"):
                self._pending.pop(key, None)
                log.error("order %s %s: %s", order_id, status, details.get("status_message"))
                return None
            _time.sleep(1)

        # Unfilled limit order: cancel and chase once with a MARKET order.
        log.warning("order %s not filled in %ds (status=%s); cancelling", order_id, self.FILL_POLL_SECONDS, status)
        try:
            self.api.cancel_order(order_id)
        except UpstoxError as exc:
            # cancel failed — the limit may still be live at the exchange. Keep it
            # marked pending so we never place a duplicate, and force a reconcile.
            self.pending_unconfirmed = True
            log.error("cancel failed for %s: %s — kept pending to avoid a duplicate", order_id, exc)
            return None
        if order_type != "LIMIT":
            self._pending.pop(key, None)  # market order we cancelled; nothing resting
            return None

        log.info("retrying %s %s as MARKET", side, instrument_key)
        try:
            order_id = self.api.place_order(
                instrument_key=instrument_key, quantity=qty,
                transaction_type=side, order_type="MARKET", product=self.cfg.product,
            )
        except UpstoxError as exc:
            self._pending.pop(key, None)  # limit was cancelled, market never placed
            log.error("market retry failed: %s", exc)
            return None
        self._pending[key] = order_id
        self._recent_orders[instrument_key] = _time.time()
        # poll for the market fill for up to MARKET_CONFIRM_SECONDS
        deadline2 = _time.time() + self.MARKET_CONFIRM_SECONDS
        while _time.time() < deadline2:
            try:
                details = self.api.order_details(order_id)
                st = (details.get("status") or "").lower()
                if st == "complete":
                    self._pending.pop(key, None)
                    avg = details.get("average_price")
                    return float(avg) if avg else (ltp or 0.0)
                if st in ("rejected", "cancelled"):
                    self._pending.pop(key, None)
                    log.error("market retry %s %s: %s", order_id, st, details.get("status_message"))
                    return None
            except UpstoxError:
                pass
            _time.sleep(1)
        # Unknown outcome: the order may still fill at the exchange. Leave it
        # marked pending (blocks duplicates) and flag for reconciliation.
        self.pending_unconfirmed = True
        log.error(
            "market retry %s not confirmed in %ds — kept pending, flagging for reconcile "
            "(may fill at the exchange)", order_id, self.MARKET_CONFIRM_SECONDS,
        )
        return None

    def _fill_buy(self, pipeline_id, instrument_key, qty, price_hint):
        ltp = price_hint
        try:
            ltp = self.api.ltp_single(instrument_key) or price_hint
        except UpstoxError:
            pass
        return self._place_and_wait(pipeline_id, instrument_key, qty, "BUY", ltp)

    def _fill_sell(self, pos, price_hint):
        ltp = price_hint
        try:
            ltp = self.api.ltp_single(pos.instrument_key) or price_hint
        except UpstoxError:
            pass
        return self._place_and_wait(pos.pipeline_id, pos.instrument_key, pos.qty, "SELL", ltp)
