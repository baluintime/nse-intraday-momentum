"""Live data plumbing for the NSE Intraday Momentum web page.

:class:`MomentumService` fetches candles, quotes and depth from Upstox for each
watchlist stock, assembles a :class:`~nsemomentum.momentum.CandidateInput` for both
the 1-minute and 5-minute timeframes, and runs
:func:`~nsemomentum.momentum.evaluate_candidate`. Results are cached briefly so
several open dashboard tabs don't multiply API calls.

The heavy lifting (the actual strategy rules) lives in
:mod:`nsemomentum.momentum`; this module is only the network + aggregation glue.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from collections import defaultdict
from datetime import datetime, time, timedelta

from .candles import Candle, TimeframeAggregator
from .config import Config, MomentumSymbol
from .fno_universe import build_fno_universe
from .momentum import (
    AVOID,
    BUY_CE,
    CandidateInput,
    MomentumEvaluation,
    evaluate_candidate,
)
from .tzutil import get_zone
from .upstox_api import UpstoxAPI, UpstoxError

log = logging.getLogger(__name__)


def symbol_score(pipelines: list[dict]) -> float:
    """Rank score for a scanned symbol (best of its timeframes). Trade-ready
    candidates sort above merely-screened ones, which sort above the rest;
    conviction breaks ties."""
    best = 0.0
    for p in pipelines:
        s = 0.0
        if p.get("action") != AVOID:
            s += 1_000
        if p.get("screen_passed"):
            s += 10_000
        if p.get("trade_ready"):
            s += 100_000
        s += (p.get("conviction") or 0.0) * 1_000
        best = max(best, s)
    return best


def scan_due(now: datetime, scan_times: list[time], last_scan_at: datetime | None) -> bool:
    """True when a scheduled scan is due: the wall clock has passed one of the
    scan times today and no scan has run since that time."""
    for t in scan_times:
        sched = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if now >= sched and (last_scan_at is None or last_scan_at < sched):
            return True
    return False


def next_scan_at(now: datetime, scan_times: list[time]) -> datetime | None:
    """The next scheduled scan datetime at/after `now` (tomorrow's first if all
    of today's have passed)."""
    if not scan_times:
        return None
    today = sorted(
        now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0) for t in scan_times
    )
    for sched in today:
        if sched > now:
            return sched
    return today[0] + timedelta(days=1)


def _by_day(candles: list[Candle]) -> dict:
    days: dict = defaultdict(list)
    for c in candles:
        days[c.ts.date()].append(c)
    return days


def _opening_window_volume(day_candles: list[Candle], open_time, minutes: int) -> float:
    """Total 1m volume in the first ``minutes`` of a session (the RVOL window)."""
    if not day_candles:
        return 0.0
    cutoff = (
        datetime.combine(day_candles[0].ts.date(), open_time) + timedelta(minutes=minutes)
    ).time()
    return sum(c.volume for c in day_candles if c.ts.time() < cutoff)


class MomentumService:
    def __init__(self, cfg: Config, api: UpstoxAPI):
        self.cfg = cfg
        self.api = api
        self.tz = get_zone(cfg.timezone)
        self.mcfg = cfg.momentum_config()
        self.ttl = max(3.0, cfg.web_refresh_seconds / 2.0)
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0
        self._hist: dict[str, tuple[str, list[Candle]]] = {}
        self.fail_cooldown = 300.0
        self._failed: dict[str, tuple[float, str]] = {}
        # --- auto F&O universe scanning ---
        self.auto = cfg.mom_auto_universe
        self.top_n = cfg.mom_top_n
        self.scan_times = cfg.mom_scan_times
        self._universe: list[MomentumSymbol] = []
        self._universe_day: str | None = None
        self._locked: list[MomentumSymbol] = []
        self._ranked: list[dict] = []
        self._scanning = False
        self._scan_progress = (0, 0)
        self._scan_lock = threading.Lock()
        self._last_scan_at: datetime | None = None
        self._scan_error: str | None = None

    # ------------------------------------------------------------- data

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _historical(self, key: str, today: str) -> list[Candle]:
        cached = self._hist.get(key)
        if cached and cached[0] == today:
            return cached[1]
        now = self._now()
        to_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=max(self.cfg.warmup_days, 14))).strftime("%Y-%m-%d")
        rows = self.api.historical_candles(key, to_date, from_date)
        candles = sorted((Candle.from_upstox(r) for r in rows), key=lambda c: c.ts)
        self._hist[key] = (today, candles)
        return candles

    def _one_min(self, key: str) -> list[Candle]:
        """Completed 1m candles: cached historical warmup + fresh intraday."""
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        candles = list(self._historical(key, today))
        rows = self.api.intraday_candles(key)
        cutoff = now.replace(second=0, microsecond=0)
        intraday = sorted(
            (
                c
                for r in rows
                if (c := Candle.from_upstox(r)).ts + timedelta(minutes=1) <= cutoff
            ),
            key=lambda c: c.ts,
        )
        last_ts = candles[-1].ts if candles else None
        candles.extend(c for c in intraday if last_ts is None or c.ts > last_ts)
        return candles

    def _futures_oi(self, futures_key: str | None) -> tuple[float | None, float | None]:
        """(start-of-day OI, latest OI) for the near-month futures, from today's
        intraday candles. ΔOI between them is the intraday build-up."""
        if not futures_key:
            return None, None
        try:
            rows = self.api.intraday_candles(futures_key)
        except UpstoxError as exc:
            log.debug("futures OI fetch failed for %s: %s", futures_key, exc)
            return None, None
        candles = sorted((Candle.from_upstox(r) for r in rows), key=lambda c: c.ts)
        candles = [c for c in candles if c.oi]
        if not candles:
            return None, None
        return candles[0].oi, candles[-1].oi

    # ------------------------------------------------------- per symbol

    def _build_input(
        self, sym: MomentumSymbol, timeframe: int, one_min: list[Candle],
        depth: tuple[float | None, float | None], oi_prev: float | None, oi_now: float | None,
    ) -> CandidateInput:
        today = self._now().date()
        days = _by_day(one_min)
        prev_days = sorted(d for d in days if d < today)
        prev_close = prev_day_high = prev_day_low = None
        if prev_days:
            last_day = days[prev_days[-1]]
            prev_close = last_day[-1].close
            prev_day_high = max(c.high for c in last_day)
            prev_day_low = min(c.low for c in last_day)

        # opening-window RVOL (always from 1m data, timeframe-independent)
        open_time = self.cfg.market_open
        win = self.mcfg.opening_window_minutes
        today_1m = days.get(today, [])
        opening_vol = _opening_window_volume(today_1m, open_time, win)
        prior_vols = [
            _opening_window_volume(days[d], open_time, win) for d in prev_days[-10:]
        ]
        avg_opening = (sum(prior_vols) / len(prior_vols)) if prior_vols else None

        # timeframe series
        if timeframe == 1:
            trend_candles = one_min
            today_candles = today_1m
        else:
            agg = TimeframeAggregator(timeframe)
            trend_candles = [done for c in one_min if (done := agg.feed(c))]
            agg2 = TimeframeAggregator(timeframe)
            today_candles = [done for c in today_1m if (done := agg2.feed(c))]

        return CandidateInput(
            symbol=sym.name,
            timeframe=f"{timeframe}m",
            today_candles=today_candles,
            trend_candles=trend_candles,
            prev_close=prev_close,
            prev_day_high=prev_day_high,
            prev_day_low=prev_day_low,
            opening_window_volume=opening_vol,
            avg_opening_window_volume=avg_opening,
            futures_oi_prev=oi_prev,
            futures_oi_now=oi_now,
            total_buy_qty=depth[0],
            total_sell_qty=depth[1],
        )

    def _evaluate_symbol(self, sym: MomentumSymbol) -> dict:
        one_min = self._one_min(sym.key)
        # depth from the equity full quote; OI from the futures intraday candles
        depth: tuple[float | None, float | None] = (None, None)
        try:
            quotes = self.api.full_quote([sym.key])
            depth = self.api.depth_totals(quotes.get(sym.key, {}))
        except UpstoxError as exc:
            log.debug("depth fetch failed for %s: %s", sym.name, exc)
        oi_prev, oi_now = self._futures_oi(sym.futures_key)

        ltp = one_min[-1].close if one_min else None
        today = self._now().date()
        prev_days = sorted(d for d in _by_day(one_min) if d < today)
        prev_close = _by_day(one_min)[prev_days[-1]][-1].close if prev_days else None
        change_pct = None
        if prev_close and ltp:
            change_pct = round((ltp - prev_close) / prev_close * 100, 2)
        pipelines = []
        for tf in self.cfg.mom_timeframes_minutes:
            inp = self._build_input(sym, tf, one_min, depth, oi_prev, oi_now)
            ev = evaluate_candidate(inp, self.mcfg)
            pipelines.append(_eval_payload(ev))
        return {
            "name": sym.name,
            "key": sym.key,
            "has_futures": bool(sym.futures_key),
            "ltp": round(ltp, 2) if ltp else None,
            "change_pct": change_pct,
            "pipelines": pipelines,
            "error": None,
        }

    # ------------------------------------------------------ universe scan

    def _get_universe(self) -> list[MomentumSymbol]:
        today = self._now().strftime("%Y-%m-%d")
        if self._universe_day == today and self._universe:
            return self._universe
        universe = build_fno_universe(today=self._now().date())
        if self.cfg.mom_universe_limit > 0:
            universe = universe[: self.cfg.mom_universe_limit]
        self._universe, self._universe_day = universe, today
        return universe

    def scan(self) -> None:
        """Evaluate the whole F&O universe, rank it, and lock the top-N
        candidates. Heavy — run in a background thread, once per scheduled time
        (09:15 & 13:00 by default) or on manual trigger."""
        with self._scan_lock:
            if self._scanning:
                return
            self._scanning = True
            self._scan_progress = (0, 0)
            self._scan_error = None
        try:
            universe = self._get_universe()
            results: list[tuple[float, MomentumSymbol, dict]] = []
            for i, sym in enumerate(universe):
                try:
                    data = self._evaluate_symbol(sym)
                except UpstoxError as exc:
                    log.debug("scan: %s failed: %s", sym.name, exc)
                    self._scan_progress = (i + 1, len(universe))
                    continue
                score = symbol_score(data.get("pipelines") or [])
                data["score"] = round(score, 1)
                results.append((score, sym, data))
                self._scan_progress = (i + 1, len(universe))
            results.sort(key=lambda t: t[0], reverse=True)
            self._locked = [sym for _s, sym, _d in results[: self.top_n]]
            self._ranked = [
                {
                    "name": d["name"],
                    "score": d.get("score"),
                    "action": max(
                        (p.get("action_label") for p in (d.get("pipelines") or [])),
                        key=lambda a: 0 if a == "AVOID" else 1, default="AVOID",
                    ),
                    "trade_ready": any(p.get("trade_ready") for p in (d.get("pipelines") or [])),
                }
                for _s, _sym, d in results[:15]
            ]
            self._last_scan_at = self._now()
            log.info(
                "F&O scan complete: %d scanned, locked top %d: %s",
                len(results), len(self._locked), ", ".join(s.name for s in self._locked),
            )
        except Exception as exc:  # noqa: BLE001 - surface on the page, don't crash the thread
            self._scan_error = f"{type(exc).__name__}: {exc}"
            log.exception("F&O universe scan failed")
        finally:
            self._scanning = False
            self.invalidate()

    def locked_symbols(self) -> list[MomentumSymbol]:
        """The current locked top-N picks (a copy) — consumed by the trading
        engine to decide which stocks to trade this session."""
        return list(self._locked)

    def scan_async(self) -> bool:
        """Kick off a scan in a background thread. False if one is already running."""
        if self._scanning:
            return False
        threading.Thread(target=self.scan, daemon=True, name="momentum-scan").start()
        return True

    def run_scheduler(self, stop: threading.Event) -> None:
        """Trigger a scan at each configured scan time on weekdays (and once at
        startup if the session has already opened). Blocks until `stop` is set."""
        log.info(
            "momentum scheduler running; scan times: %s",
            ", ".join(t.strftime("%H:%M") for t in self.scan_times),
        )
        while not stop.is_set():
            now = self._now()
            if now.weekday() < 5 and self.api.has_token and scan_due(now, self.scan_times, self._last_scan_at):
                self.scan()
            stop.wait(30)

    # ---------------------------------------------------------- payload

    def payload(self) -> dict:
        with self._lock:
            if not self.api.has_token:
                return self._envelope(connected=False, symbols=[])
            if self._cached is not None and _time.monotonic() - self._cached_at < self.ttl:
                return self._cached
            data = self._build()
            self._cached, self._cached_at = data, _time.monotonic()
            return data

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def _market_status(self, now: datetime) -> str:
        if now.weekday() >= 5:
            return "CLOSED"
        t = now.time()
        if self.cfg.market_open <= t <= self.cfg.market_close:
            return "OPEN"
        return "PRE-OPEN" if t < self.cfg.market_open else "CLOSED"

    def _source_symbols(self) -> list[MomentumSymbol]:
        """The symbols shown/managed right now: the locked top-N in auto mode,
        else the fixed watchlist."""
        return self._locked if self.auto else self.cfg.enabled_momentum_symbols

    def _build(self) -> dict:
        symbols_payload: list[dict] = []
        error: str | None = None
        for sym in self._source_symbols():
            paused = self._failed.get(sym.key)
            if paused and _time.monotonic() - paused[0] < self.fail_cooldown:
                symbols_payload.append(
                    {"name": sym.name, "key": sym.key, "ltp": None, "pipelines": [],
                     "error": paused[1] + " (retry paused)"}
                )
                error = error or "Some symbols failed to load — see cards."
                continue
            try:
                symbols_payload.append(self._evaluate_symbol(sym))
                self._failed.pop(sym.key, None)
            except UpstoxError as exc:
                msg = str(exc)[:200]
                self._failed[sym.key] = (_time.monotonic(), msg)
                symbols_payload.append(
                    {"name": sym.name, "key": sym.key, "ltp": None, "pipelines": [], "error": msg}
                )
                error = error or "Some symbols failed to load — see cards."

        # sort: actionable (trade-ready) first, then by best conviction
        def sort_key(s: dict) -> tuple:
            pipes = s.get("pipelines") or []
            ready = any(p.get("trade_ready") for p in pipes)
            conv = max((p.get("conviction", 0) for p in pipes), default=0)
            return (0 if ready else 1, -conv)

        symbols_payload.sort(key=sort_key)
        return self._envelope(connected=True, symbols=symbols_payload, error=error)

    def _scan_meta(self, now: datetime) -> dict:
        nxt = next_scan_at(now, self.scan_times) if self.auto else None
        return {
            "mode": "universe" if self.auto else "watchlist",
            "top_n": self.top_n,
            "scan_times": [t.strftime("%H:%M") for t in self.scan_times],
            "universe_size": len(self._universe),
            "scanning": self._scanning,
            "progress": list(self._scan_progress),
            "last_scan_at": self._last_scan_at.strftime("%H:%M:%S") if self._last_scan_at else None,
            "next_scan_at": nxt.strftime("%a %H:%M") if nxt else None,
            "locked": [s.name for s in self._locked],
            "ranked": self._ranked,
            "error": self._scan_error,
        }

    def _envelope(self, connected: bool, symbols: list[dict], error: str | None = None) -> dict:
        now = self._now()
        return {
            "connected": connected,
            "generated_at": now.strftime("%H:%M:%S"),
            "generated_date": now.strftime("%a, %d %b %Y"),
            "refresh_seconds": self.cfg.web_refresh_seconds,
            "scan": self._scan_meta(now),
            "market": {
                "status": self._market_status(now),
                "session": f"{self.cfg.market_open.strftime('%H:%M')}–{self.cfg.market_close.strftime('%H:%M')} IST",
            },
            "strategy": {
                "name": "NSE Intraday Momentum",
                "timeframes": [f"{tf}m" for tf in self.cfg.mom_timeframes_minutes],
                "thresholds": {
                    "gap_pct": self.mcfg.min_gap_pct,
                    "rvol": self.mcfg.min_rvol,
                    "oi_change_pct": self.mcfg.min_oi_change_pct,
                    "depth_ratio": self.mcfg.min_depth_ratio,
                    "delta": [self.mcfg.delta_min, self.mcfg.delta_max],
                },
            },
            "error": error,
            "symbols": symbols,
        }


def _eval_payload(ev: MomentumEvaluation) -> dict:
    want_ce = ev.action == BUY_CE
    trend = ev.trend
    return {
        "timeframe": ev.timeframe,
        "ready": ev.ready,
        "action": ev.action,
        "action_label": {"BUY_CE": "BUY CALL", "BUY_PE": "BUY PUT", "AVOID": "AVOID"}[ev.action],
        "classification": ev.classification,
        "screen_passed": ev.screen_passed,
        "trade_ready": ev.trade_ready,
        "conviction": round(ev.conviction, 3),
        "blocked_reason": ev.blocked_reason,
        "exit_triggered": ev.exit_triggered,
        "checks": [
            {"name": c.name, "status": c.status, "detail": c.detail} for c in ev.checks
        ],
        "kumo": (
            {
                "kijun": round(trend.kijun, 2) if trend.kijun is not None else None,
                "cloud_top": round(trend.cloud_top, 2) if trend.cloud_top is not None else None,
                "cloud_bottom": round(trend.cloud_bottom, 2) if trend.cloud_bottom is not None else None,
                "zone": "above" if trend.above_cloud else "below" if trend.below_cloud else "inside",
            }
            if trend and trend.ready
            else None
        ),
        "want_ce": want_ce,
    }
