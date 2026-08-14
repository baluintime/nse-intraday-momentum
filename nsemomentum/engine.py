"""Live/paper trading engine.

Data flow per enabled index:

  Upstox intraday 1m candle API --poll--> new completed 1m candles
      -> 1m pipeline (Ichimoku on 1m closes)
      -> 5m aggregator -> 5m pipeline (Ichimoku on 5m closes)

Signals -> ITM option selection (delta 0.65-0.75, nearest expiry)
        -> PaperBroker (simulated) or LiveBroker (real Upstox orders).

Entries execute immediately after the triggering candle closes — i.e. at the
open of the subsequent candle, per the spec. Exits execute immediately.
"""

from __future__ import annotations

import logging
import re
import threading
import time as _time
from datetime import datetime, time, timedelta

from .broker import BaseBroker, LiveBroker, PaperBroker
from .candles import Candle, CandleSeries, TimeframeAggregator
from .config import Config, IndexConfig
from .ichimoku import compute_state, long_exit, short_exit
from .options import OptionSelector
from .strategy import ENTER_LONG, EXIT, Pipeline, Signal, StrategyConfig, build_strategy_config
from .tzutil import get_zone
from .upstox_api import UpstoxAPI, UpstoxError

log = logging.getLogger(__name__)


class IndexRunner:
    """All per-index state: 1m feed tracking, aggregators and pipelines."""

    def __init__(self, index: IndexConfig, cfg: Config, sc: "StrategyConfig"):
        self.index = index
        self.cfg = cfg
        self.one_min = CandleSeries()  # dedupe/tracking of raw 1m feed
        self.aggregators = {
            tf: TimeframeAggregator(tf) for tf in cfg.timeframes_minutes if tf != 1
        }
        self.pipelines: dict[int, Pipeline] = {
            tf: Pipeline(index.name, tf, sc) for tf in cfg.timeframes_minutes
        }
        self.warmed = False        # historical/intraday warmup done for this runner
        self.momentum_sourced = False  # created from a momentum top-N pick, not config
        # opening-range breakout gate (per index, per day)
        self.or_date = None  # type: ignore[assignment]
        self.or_high: float | None = None
        self.or_low: float | None = None
        self.or_unlocked = False

    def _or_window_end(self, d) -> time:
        end = datetime.combine(d, self.cfg.market_open) + timedelta(minutes=self.cfg.opening_range_minutes)
        return end.time()

    def update_opening_range(self, candle: Candle) -> None:
        """Track today's opening-range high/low from completed 1m candles and
        unlock the index once a candle closes beyond that range."""
        if self.cfg.opening_range_minutes <= 0:
            return
        d = candle.ts.date()
        if self.or_date != d:  # new session — reset
            self.or_date = d
            self.or_high = self.or_low = None
            self.or_unlocked = False
        t = candle.ts.time()
        if t < self.cfg.market_open:
            return
        if t < self._or_window_end(d):
            self.or_high = candle.high if self.or_high is None else max(self.or_high, candle.high)
            self.or_low = candle.low if self.or_low is None else min(self.or_low, candle.low)
        elif not self.or_unlocked:
            if self.or_high is None:
                self.or_unlocked = True  # no range formed (data gap) — don't gate
            elif candle.close > self.or_high or candle.close < self.or_low:
                self.or_unlocked = True


class Engine:
    def __init__(self, cfg: Config, api: UpstoxAPI, momentum=None):
        self.cfg = cfg
        self.api = api
        self.tz = get_zone(cfg.timezone)
        self.sc = build_strategy_config(cfg)
        self.params = self.sc.ich
        self.selector = OptionSelector(api, cfg)
        self.broker: BaseBroker = (
            LiveBroker(cfg, api) if cfg.mode == "live" else PaperBroker(cfg, api)
        )
        # momentum-link: trade the scanner's locked top-N stocks via the Ichimoku
        # pipeline. When on with no_index_trade, the config indices are not traded.
        self.momentum = momentum if cfg.mom_trade_with_ichimoku else None
        self._momentum_names: set[str] = set()
        self.runners: list[IndexRunner] = []
        self._runner_by_name: dict[str, IndexRunner] = {}
        if not (self.momentum and cfg.mom_no_index_trade):
            for ix in cfg.enabled_instruments:
                self._add_runner(ix, momentum_sourced=False)
        self.trades_today: dict[str, int] = {}
        self._squared_off = False
        self._halted = False
        self._halt_reason = ""
        self._sync_ok = True
        self._position_mismatch: list[dict] = []
        self._stop = threading.Event()
        # latest entry-skip reason per pipeline, surfaced on the dashboard
        self.last_skips: dict[str, dict] = {}

    def _record_skip(self, pid: str, reason: str, direction: str | None = None) -> None:
        self.last_skips[pid] = {"reason": reason, "direction": direction, "at": _time.time()}

    def recent_skips(self, ttl: float = 90.0) -> list[dict]:
        """Entry-skip notes from the last `ttl` seconds, for the dashboard."""
        now = _time.time()
        out = []
        for pid, s in self.last_skips.items():
            age = now - s["at"]
            if age <= ttl:
                out.append({"pipeline": pid, "reason": s["reason"],
                            "direction": s["direction"], "age_s": round(age)})
        return out

    # -------------------------------------------------- runner management

    def _add_runner(self, index: IndexConfig, momentum_sourced: bool) -> IndexRunner:
        runner = IndexRunner(index, self.cfg, build_strategy_config(self.cfg, index))
        runner.momentum_sourced = momentum_sourced
        self.runners.append(runner)
        self._runner_by_name[index.name] = runner
        if momentum_sourced:
            self._momentum_names.add(index.name)
        return runner

    def _remove_runner(self, name: str) -> None:
        runner = self._runner_by_name.pop(name, None)
        if runner is not None:
            self.runners = [r for r in self.runners if r is not runner]
        self._momentum_names.discard(name)

    def _has_open_position(self, runner: IndexRunner) -> bool:
        return any(self.broker.position(p.pipeline_id) is not None for p in runner.pipelines.values())

    def sync_momentum_runners(self) -> None:
        """Reconcile the tradeable runner set with the momentum scanner's current
        locked top-N picks: add + warm up new picks, keep dropped picks only while
        they hold a position (exit-only, no new entries), remove the rest."""
        if self.momentum is None:
            return
        try:
            picks = self.momentum.locked_symbols()
        except Exception as exc:  # noqa: BLE001 - never let scanner state crash the loop
            log.debug("could not read momentum picks: %s", exc)
            return
        desired = {s.name: s for s in picks}
        for name, sym in desired.items():
            runner = self._runner_by_name.get(name)
            if runner is None:
                ix = IndexConfig(name=sym.name, key=sym.key, options_available=True, trade_enabled=True)
                runner = self._add_runner(ix, momentum_sourced=True)
                self._warmup_runner(runner)
                log.info("momentum: now trading %s (entered top-%d)", name, self.momentum.top_n)
            elif runner.momentum_sourced and not runner.index.trade_enabled:
                runner.index.trade_enabled = True  # re-entered the top-N
        for name in list(self._momentum_names):
            if name in desired:
                continue
            runner = self._runner_by_name.get(name)
            if runner is None:
                self._momentum_names.discard(name)
            elif self._has_open_position(runner):
                if runner.index.trade_enabled:
                    runner.index.trade_enabled = False
                    log.info("momentum: %s left top-%d — holding to manage exit (no new entries)", name, self.momentum.top_n)
            else:
                self._remove_runner(name)
                log.info("momentum: %s left top-%d — dropped (no open position)", name, self.momentum.top_n)

    # ------------------------------------------------------------ warmup

    def _warmup_runner(self, runner: IndexRunner) -> None:
        now = self._now()
        to_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=self.cfg.warmup_days)).strftime("%Y-%m-%d")
        key = runner.index.key
        rows: list[list] = []
        try:
            rows.extend(self.api.historical_candles(key, to_date, from_date))
        except UpstoxError as exc:
            log.warning("historical warmup failed for %s: %s", runner.index.name, exc)
        try:
            rows.extend(self.api.intraday_candles(key))
        except UpstoxError as exc:
            log.warning("intraday warmup failed for %s: %s", runner.index.name, exc)

        # drop the in-progress candle: only intervals that have fully ended
        cutoff = now.replace(second=0, microsecond=0)
        candles = sorted(
            (
                c
                for r in rows
                if (c := Candle.from_upstox(r)).ts + timedelta(minutes=1) <= cutoff
            ),
            key=lambda c: c.ts,
        )
        new_1m: list[Candle] = []
        for c in candles:
            if runner.one_min.append(c):
                new_1m.append(c)
                runner.update_opening_range(c)
        if 1 in runner.pipelines:
            runner.pipelines[1].warmup(new_1m)
        for tf, agg in runner.aggregators.items():
            agg_candles = [done for c in new_1m if (done := agg.feed(c))]
            runner.pipelines[tf].warmup(agg_candles)
        runner.warmed = True
        log.info(
            "%s warmup: %d x 1m candles (%s)",
            runner.index.name,
            len(runner.one_min),
            ", ".join(f"{tf}m series={len(p.series)}" for tf, p in runner.pipelines.items()),
        )

    def warmup(self) -> None:
        if self.cfg.mode == "live":
            # seed the live book from Upstox so square-off closes what really exists
            try:
                n = self.broker.seed_from_upstox()
                if n:
                    log.warning("seeded %d untracked Upstox position(s) into the live book at startup", n)
            except Exception as exc:  # noqa: BLE001
                log.warning("startup position seeding failed: %s", exc)
        # pull in the current momentum picks (these warm themselves as they are added)
        self.sync_momentum_runners()
        for runner in list(self.runners):
            if not runner.warmed:
                self._warmup_runner(runner)

    # ------------------------------------------------------------ session

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _between(self, t: time, start: time, end: time) -> bool:
        return start <= t <= end

    def run(self, stop_event: threading.Event | None = None) -> None:
        """Run until market close or until `stop_event` is set (e.g. from the
        web dashboard). Stopping does NOT square off open positions."""
        if stop_event is not None:
            self._stop = stop_event
        mode_desc = "momentum top-%d stocks" % self.momentum.top_n if self.momentum else f"{len(self.runners)} instruments"
        log.info("engine starting in %s mode (%s)", self.cfg.mode.upper(), mode_desc)
        self.warmup()
        while not self._stop.is_set():
            now = self._now()
            t = now.time()
            if t >= self.cfg.market_close:
                log.info("market closed — stopping")
                break
            if t < self.cfg.market_open:
                wait = (
                    datetime.combine(now.date(), self.cfg.market_open, self.tz) - now
                ).total_seconds()
                log.info("waiting %.0fs for market open", wait)
                self._stop.wait(min(wait + 1, 300))
                continue
            if t >= self.cfg.square_off and not self._squared_off:
                self.square_off_all("square-off time")
                self._squared_off = True
            self.poll_once()
            self.reconcile_positions()
            self.check_partial_targets()
            self.check_profit_target()
            self._stop.wait(self.cfg.poll_interval_seconds)
        if self._stop.is_set():
            log.info("engine stopped on request (open positions are left untouched)")
            return
        # natural end of session: make sure nothing is left open
        if not self._squared_off and self.broker.open_positions():
            self.square_off_all("session end")

    def poll_once(self) -> None:
        """Fetch today's 1m candles for every tradeable instrument and process new
        completed ones. First reconcile the runner set with the momentum picks."""
        self.sync_momentum_runners()
        for runner in list(self.runners):
            try:
                rows = self.api.intraday_candles(runner.index.key)
            except UpstoxError as exc:
                log.warning("candle poll failed for %s: %s", runner.index.name, exc)
                continue
            cutoff = self._now().replace(second=0, microsecond=0)
            fresh = sorted(
                (
                    c
                    for r in rows
                    # candle must be complete: its interval must have ended
                    if (c := Candle.from_upstox(r)).ts + timedelta(minutes=1) <= cutoff
                ),
                key=lambda c: c.ts,
            )
            for candle in fresh:
                if not runner.one_min.append(candle):
                    continue
                # update the opening-range gate before any entry is evaluated,
                # so the breakout candle itself can unlock and trade
                runner.update_opening_range(candle)
                self._process_candle(runner, 1, candle)
                for tf, agg in runner.aggregators.items():
                    done = agg.feed(candle)
                    if done is not None:
                        self._process_candle(runner, tf, done)

    def _process_candle(self, runner: IndexRunner, tf: int, candle: Candle) -> None:
        pipeline = runner.pipelines.get(tf)
        if pipeline is None:
            return
        side = self.broker.position_side(pipeline.pipeline_id)
        for signal in pipeline.on_candle_close(candle, side):
            self._execute(runner, signal)

    # ---------------------------------------------------------- execution

    def _execute(self, runner: IndexRunner, signal: Signal) -> None:
        pid = signal.pipeline_id
        if signal.action == EXIT:
            self.broker.exit(pid, price_hint=None, note="signal", underlying_spot=signal.candle.close)
            return

        # entries
        direction = "LONG" if signal.action == ENTER_LONG else "SHORT"
        spot = signal.candle.close
        now_t = self._now().time()
        if not self._sync_ok:
            self._record_skip(pid, "position mismatch — trading paused", direction)
            return
        if self._halted:
            self._record_skip(pid, self._halt_reason or "trading halted", direction)
            return
        if self._squared_off or now_t >= self.cfg.entry_cutoff:
            log.info("%s entry skipped: past entry cutoff", pid)
            self._record_skip(pid, "past entry cutoff", direction)
            return
        if self.cfg.max_daily_loss > 0 and self.broker.realized_pnl_today() <= -self.cfg.max_daily_loss:
            log.warning("%s entry skipped: daily loss limit hit (pnl=%.2f)", pid, self.broker.realized_pnl_today())
            self._record_skip(pid, "daily loss limit reached", direction)
            return
        if (
            self.cfg.max_trades_per_day_per_pipeline > 0
            and self.trades_today.get(pid, 0) >= self.cfg.max_trades_per_day_per_pipeline
        ):
            log.info("%s entry skipped: max trades/day reached", pid)
            self._record_skip(pid, "max trades/day reached", direction)
            return
        if self.cfg.opening_range_minutes > 0 and not runner.or_unlocked:
            log.info(
                "%s entry skipped: waiting for opening-range breakout (OR %s–%s)",
                pid,
                f"{runner.or_low:.1f}" if runner.or_low is not None else "?",
                f"{runner.or_high:.1f}" if runner.or_high is not None else "?",
            )
            self._record_skip(pid, "waiting for opening-range breakout", direction)
            return

        if not runner.index.options_available:
            log.info("%s: %s has no listed options — signal only, no trade placed", pid, runner.index.name)
            return
        if not runner.index.trade_enabled:
            log.info("%s: trading is toggled OFF for %s — signal only", pid, runner.index.name)
            return
        sel = self.selector.select_itm(runner.index.key, direction, spot)
        if sel is None:
            log.warning("%s: no suitable ITM %s found; entry skipped", pid, "CALL" if direction == "LONG" else "PUT")
            self._record_skip(pid, "no liquid strike — entry skipped", direction)
            return
        qty = sel.lot_size * self.cfg.lots_per_trade
        log.info(
            "%s %s -> BUY %s x%d (strike %.0f, delta %s, exp %s, vol %s, oi %s, spread %s)",
            pid, direction, sel.trading_symbol, qty, sel.strike,
            f"{sel.delta:.2f}" if sel.delta is not None else "n/a", sel.expiry,
            f"{sel.volume:.0f}" if sel.volume is not None else "n/a",
            f"{sel.oi:.0f}" if sel.oi is not None else "n/a",
            f"{sel.spread_pct:.1f}%" if sel.spread_pct is not None else "n/a",
        )
        pos = self.broker.enter(
            pid, sel.instrument_key, sel.trading_symbol, qty, direction, sel.ltp,
            underlying_spot=spot, strike=sel.strike, lot_size=sel.lot_size,
        )
        if pos is not None:
            self.trades_today[pid] = self.trades_today.get(pid, 0) + 1
            self.last_skips.pop(pid, None)  # cleared: this pipeline just entered

    @staticmethod
    def _alnum(text: str) -> str:
        """Uppercase, stripped of every non-alphanumeric char — so an option
        trading symbol and its underlying name compare cleanly regardless of
        spaces, hyphens or ampersands (BAJAJ-AUTO / M&M equity options included)."""
        return re.sub(r"[^A-Z0-9]", "", (text or "").upper())

    def _runner_for_symbol(self, symbol: str) -> "IndexRunner | None":
        """Map an option trading symbol to its runner by underlying prefix. Works
        for both index options ('BANKNIFTY 56100 CE') and equity options
        ('RELIANCE 3000 CE', 'BAJAJ-AUTO 9000 PE')."""
        norm = self._alnum(symbol)
        # longest underlying first so NIFTY doesn't shadow NIFTYNXT50 / BANKNIFTY,
        # and INFY doesn't shadow a hypothetical INFY-prefixed longer symbol
        for runner in sorted(self.runners, key=lambda r: -len(self._alnum(r.index.name))):
            rn = self._alnum(runner.index.name)
            if rn and norm.startswith(rn):
                return runner
        return None

    def _heal_orphan(self, key: str, symbol: str, qty: int, avg: float) -> str:
        """An untracked Upstox position. If a flat pipeline's current signal still
        supports holding it, adopt it (strategy will manage the exit); otherwise
        square it off. Never adopts without a real entry price, and never squares
        off an instrument we just traded (avoids a double-sell race)."""
        # Fix 2: if we placed an order on this instrument moments ago, its result
        # hasn't reflected on Upstox yet — wait a cycle rather than acting again.
        if self.broker.recently_ordered(key):
            log.info("reconcile: %s traded very recently — deferring heal one cycle", symbol)
            return "wait"

        direction = "LONG" if symbol.strip().upper().endswith("CE") else "SHORT"
        # Fix 1: resolve a trustworthy entry price; never adopt/record at 0.
        entry = float(avg) if avg and avg > 0 else 0.0
        if entry <= 0:
            try:
                entry = float(self.api.ltp_single(key) or 0.0)
            except Exception:  # noqa: BLE001
                entry = 0.0

        runner = self._runner_for_symbol(symbol)
        if runner is not None and entry > 0:
            for pipeline in runner.pipelines.values():
                pid = pipeline.pipeline_id
                if self.broker.position(pid) is not None or not pipeline.series.candles:
                    continue
                state = compute_state(pipeline.series.highs, pipeline.series.lows, self.params)
                if state is None:
                    continue
                close = pipeline.series.candles[-1].close
                still_valid = (not long_exit(close, state)) if direction == "LONG" else (not short_exit(close, state))
                if still_valid:
                    self.broker.adopt_position(pid, key, symbol, qty, entry, direction)
                    return "kept"
        # no pipeline can manage it (or no valid price to adopt at) — close it
        self.broker.square_off_instrument(key, symbol, qty, price_hint=(entry or None))
        return "squared"

    def _drop_phantom(self, key: str, excess: int) -> None:
        """App holds more than Upstox — those were closed outside the app; drop them."""
        for pos in list(self.broker.open_positions()):
            if excess <= 0:
                break
            if pos.instrument_key == key:
                self.broker.drop_position(pos.pipeline_id, "closed externally (Upstox flat)")
                excess -= pos.qty

    def reconcile_positions(self) -> bool:
        """Live only: compare the app book to Upstox's real positions and
        self-heal — adopt an orphan the strategy would still hold, square off one
        it wouldn't, drop phantoms Upstox has closed. Pause entries only if a
        mismatch remains after healing. Returns True if in sync."""
        if self.cfg.mode != "live":
            self._sync_ok = True
            return True
        mismatches = self.broker.reconcile()
        for m in mismatches:
            try:
                if m["upstox_qty"] > m["app_qty"]:
                    self._heal_orphan(m["instrument"], m["symbol"], m["upstox_qty"] - m["app_qty"], m.get("avg", 0.0))
                elif m["app_qty"] > m["upstox_qty"]:
                    self._drop_phantom(m["instrument"], m["app_qty"] - m["upstox_qty"])
            except Exception as exc:  # noqa: BLE001 - healing is best-effort; stay paused if it fails
                log.error("reconcile heal failed for %s: %s", m.get("symbol"), exc)
        if mismatches:
            mismatches = self.broker.reconcile()  # re-check after healing
        unconfirmed = getattr(self.broker, "pending_unconfirmed", False)
        self._position_mismatch = mismatches
        if mismatches:
            self._sync_ok = False
            log.error("position mismatch remains after heal — entries PAUSED: %s", mismatches)
        elif unconfirmed:
            self.broker.pending_unconfirmed = False
            self._sync_ok = True
            log.info("unconfirmed fill reconciled — Upstox and app agree; resuming")
        else:
            self._sync_ok = True
        return self._sync_ok

    def check_partial_targets(self) -> None:
        """Book a partial profit (config.partial_exit_fraction) on any open option
        position whose premium has gained partial_target_pct. Fires once per
        position; needs >= 2 lots to split."""
        pct = self.cfg.partial_target_pct
        if pct <= 0:
            return
        _, detail = self.broker.mark_to_market()
        for pos in list(self.broker.open_positions()):
            if pos.partial_taken or not pos.lot_size or pos.entry_price <= 0:
                continue
            ltp = (detail.get(pos.pipeline_id) or {}).get("ltp")
            if ltp is None:
                continue
            gain_pct = (ltp - pos.entry_price) / pos.entry_price * 100.0
            if gain_pct >= pct:
                log.info("%s +%.1f%% premium — booking partial (%.0f%%)",
                         pos.pipeline_id, gain_pct, self.cfg.partial_exit_fraction * 100)
                self.broker.partial_exit(pos.pipeline_id, self.cfg.partial_exit_fraction, price_hint=ltp)

    def check_profit_target(self) -> bool:
        """If total profit (realized + unrealized) has reached the daily target,
        square off everything at market and halt trading for the day. Returns
        True if the target was hit this call."""
        target = self.cfg.daily_profit_target
        if target <= 0 or self._halted:
            return False
        realized = self.broker.realized_pnl_today()
        unrealized, _ = self.broker.mark_to_market()
        total = realized + unrealized
        if total >= target:
            log.warning(
                "daily profit target reached: total %.2f (realized %.2f + unrealized %.2f) >= %.2f "
                "— squaring off and halting for the day",
                total, realized, unrealized, target,
            )
            self.square_off_all("profit target")
            self._halted = True
            self._halt_reason = f"daily profit target ₹{target:,.0f} reached"
            return True
        return False

    def _last_index_price(self, pipeline_id: str) -> float | None:
        """Latest completed 1m index close for the position's index (for exit logging)."""
        name = pipeline_id.split(":", 1)[0]
        for runner in self.runners:
            if runner.index.name == name and runner.one_min.candles:
                return runner.one_min.candles[-1].close
        return None

    def square_off_all(self, reason: str) -> None:
        # close what actually exists on Upstox, not the app's possibly-stale idea
        if self.cfg.mode == "live":
            try:
                self.broker.seed_from_upstox()
            except Exception as exc:  # noqa: BLE001
                log.warning("square-off: could not seed from Upstox first: %s", exc)
        for pos in self.broker.open_positions():
            log.info("square-off (%s): %s %s", reason, pos.pipeline_id, pos.symbol)
            self.broker.exit(
                pos.pipeline_id, price_hint=None, note=f"square-off:{reason}",
                underlying_spot=self._last_index_price(pos.pipeline_id),
            )
