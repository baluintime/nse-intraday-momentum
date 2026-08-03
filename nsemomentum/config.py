"""Configuration loading for NSE Momentum."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from typing import Any

import yaml


@dataclass
class IndexConfig:
    name: str
    key: str
    enabled: bool = True            # show on dashboard / compute signals
    options_available: bool = True  # index has exchange-listed option contracts
    trade_enabled: bool = True      # place trades on signals (toggle in the UI)
    # Per-index minimum cloud thickness (index points). None = inherit the global
    # strategy.min_cloud_thickness. Each index trades on a different point scale
    # (BANKNIFTY moves in the hundreds, FINNIFTY in the tens), so a single global
    # value can't gate them all — override the ones that need it.
    min_cloud_thickness: float | None = None


@dataclass
class MomentumSymbol:
    """A watchlist stock for the NSE Intraday Momentum strategy.

    ``key`` is the Upstox equity instrument key (e.g. ``NSE_EQ|INE002A01018``);
    ``futures_key`` is the optional near-month futures key used for the ΔOI
    build-up gate. Without it the OI check is reported as unavailable."""

    name: str
    key: str
    futures_key: str | None = None
    enabled: bool = True


@dataclass
class Config:
    mode: str = "paper"

    poll_interval_seconds: float = 5.0
    warmup_days: int = 7

    timezone: str = "Asia/Kolkata"
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    entry_cutoff: time = time(15, 0)
    square_off: time = time(15, 15)
    # No entries until price breaks the first N minutes' high/low (opening-range
    # breakout). 0 disables — trade from the open.
    opening_range_minutes: int = 0

    timeframes_minutes: list[int] = field(default_factory=lambda: [1, 5])
    tenkan: int = 9
    kijun: int = 26
    senkou_b: int = 52
    displacement: int = 26

    # --- optimization: entry filters (0/false disables each) ---
    use_chikou_filter: bool = False   # Chikou span clear of price `chikou_period` ago
    chikou_period: int = 26
    use_macd_filter: bool = False     # MACD histogram sign must confirm the direction
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    min_cloud_thickness: float = 0.0  # reject when |SpanA-SpanB| < this (index points)
    # --- optimization: exit restructuring ---
    # 'kijun' = soft trailing stop on Kijun close + hard stop at the opposite Kumo edge;
    # 'any_level' = original (exit on a close past any one of the four lines).
    exit_mode: str = "any_level"
    # partial profit: close this fraction once the option premium gains partial_target_pct.
    partial_target_pct: float = 0.0   # 0 disables
    partial_exit_fraction: float = 0.5

    target_delta: float = 0.70
    delta_min: float = 0.65
    delta_max: float = 0.75
    expiry: str = "nearest"
    # Skip an expiry that is this many days away or less and roll to the next
    # (avoids trading right at expiry). 0 = always use the nearest.
    min_days_to_expiry: int = 0
    order_type: str = "LIMIT"
    limit_tolerance_pct: float = 0.25
    product: str = "I"
    # liquidity guards (0 disables the check)
    min_volume: int = 0
    min_open_interest: int = 0
    max_spread_pct: float = 0.0

    lots_per_trade: int = 1
    max_trades_per_day_per_pipeline: int = 10
    max_daily_loss: float = 10000.0
    # When total profit (realized + unrealized) reaches this INR value, square off
    # everything and stop trading for the day. 0 disables.
    daily_profit_target: float = 0.0

    web_host: str = "127.0.0.1"
    web_port: int = 8080
    web_refresh_seconds: int = 10

    paper_starting_cash: float = 500000.0
    paper_slippage_pct: float = 0.05
    paper_state_file: str = "state/paper_state.json"
    paper_trade_log: str = "state/trades_paper.csv"
    live_trade_log: str = "state/trades_live.csv"

    instruments: list[IndexConfig] = field(default_factory=list)

    # --- NSE Intraday Momentum Option Strategy (BRD REQ-NSE-OPT-2026-V1) ---
    # Runs on both 1m and 5m; screens a stock watchlist for gap/RVOL/OI/depth,
    # applies the directional matrix, false-breakout filters and Ichimoku Kumo
    # trend retention. Operated from the /momentum web page.
    mom_min_gap_pct: float = 1.5
    mom_min_rvol: float = 3.0
    mom_min_oi_change_pct: float = 3.0
    mom_min_depth_ratio: float = 2.5
    mom_opening_window_minutes: int = 5
    mom_require_confirmation: bool = True
    mom_require_pdh_pdl: bool = True
    mom_require_cvd: bool = True
    mom_tenkan: int = 9
    mom_kijun: int = 26
    mom_senkou_b: int = 52
    mom_displacement: int = 26
    mom_chikou_period: int = 26
    mom_require_chikou: bool = True
    mom_require_tenkan_kijun: bool = True
    mom_delta_min: float = 0.50
    mom_delta_max: float = 0.65
    mom_target_delta: float = 0.58
    mom_max_risk_pct: float = 1.0
    mom_timeframes_minutes: list[int] = field(default_factory=lambda: [1, 5])
    momentum_symbols: list[MomentumSymbol] = field(default_factory=list)
    # Auto F&O universe mode: scan the whole NSE F&O stock universe at the
    # scheduled times, rank by conviction, and lock the top N candidates for the
    # session. When off, the fixed momentum_symbols watchlist is used instead.
    mom_auto_universe: bool = True
    mom_top_n: int = 3
    mom_scan_times: list[time] = field(default_factory=lambda: [time(9, 15), time(13, 0)])
    mom_universe_limit: int = 0  # cap symbols scanned (0 = whole universe)
    # Live/paper trading of the picks: when on, the Ichimoku engine trades the
    # momentum scanner's locked top-N stocks (ITM options on Ichimoku signals),
    # adding new picks mid-session and holding dropped picks only to manage their
    # exit. no_index=True means the engine does not trade the config indices.
    mom_trade_with_ichimoku: bool = True
    mom_no_index_trade: bool = True

    @property
    def enabled_instruments(self) -> list[IndexConfig]:
        return [i for i in self.instruments if i.enabled]

    @property
    def enabled_momentum_symbols(self) -> list[MomentumSymbol]:
        return [s for s in self.momentum_symbols if s.enabled]

    def momentum_config(self):
        """Build a :class:`nsemomentum.momentum.MomentumConfig` from these fields."""
        from .momentum import MomentumConfig

        return MomentumConfig(
            min_gap_pct=self.mom_min_gap_pct,
            min_rvol=self.mom_min_rvol,
            min_oi_change_pct=self.mom_min_oi_change_pct,
            min_depth_ratio=self.mom_min_depth_ratio,
            opening_window_minutes=self.mom_opening_window_minutes,
            require_confirmation=self.mom_require_confirmation,
            require_pdh_pdl=self.mom_require_pdh_pdl,
            require_cvd=self.mom_require_cvd,
            tenkan=self.mom_tenkan,
            kijun=self.mom_kijun,
            senkou_b=self.mom_senkou_b,
            displacement=self.mom_displacement,
            chikou_period=self.mom_chikou_period,
            require_chikou=self.mom_require_chikou,
            require_tenkan_kijun=self.mom_require_tenkan_kijun,
            delta_min=self.mom_delta_min,
            delta_max=self.mom_delta_max,
            target_delta=self.mom_target_delta,
            max_risk_pct=self.mom_max_risk_pct,
        )


def _parse_time(value: Any, default: time) -> time:
    if value is None:
        return default
    if isinstance(value, time):
        return value
    parts = str(value).split(":")
    return time(int(parts[0]), int(parts[1]))


def load_config(path: str = "config.yaml") -> Config:
    raw: dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    cfg = Config()
    cfg.mode = str(raw.get("mode", cfg.mode)).lower()

    data = raw.get("data", {}) or {}
    cfg.poll_interval_seconds = float(data.get("poll_interval_seconds", cfg.poll_interval_seconds))
    cfg.warmup_days = int(data.get("warmup_days", cfg.warmup_days))

    sess = raw.get("session", {}) or {}
    cfg.timezone = sess.get("timezone", cfg.timezone)
    cfg.market_open = _parse_time(sess.get("market_open"), cfg.market_open)
    cfg.market_close = _parse_time(sess.get("market_close"), cfg.market_close)
    cfg.entry_cutoff = _parse_time(sess.get("entry_cutoff"), cfg.entry_cutoff)
    cfg.square_off = _parse_time(sess.get("square_off"), cfg.square_off)
    cfg.opening_range_minutes = int(sess.get("opening_range_minutes", cfg.opening_range_minutes))

    strat = raw.get("strategy", {}) or {}
    cfg.timeframes_minutes = list(strat.get("timeframes_minutes", cfg.timeframes_minutes))
    cfg.tenkan = int(strat.get("tenkan", cfg.tenkan))
    cfg.kijun = int(strat.get("kijun", cfg.kijun))
    cfg.senkou_b = int(strat.get("senkou_b", cfg.senkou_b))
    cfg.displacement = int(strat.get("displacement", cfg.displacement))
    cfg.use_chikou_filter = bool(strat.get("use_chikou_filter", cfg.use_chikou_filter))
    cfg.chikou_period = int(strat.get("chikou_period", cfg.chikou_period))
    cfg.use_macd_filter = bool(strat.get("use_macd_filter", cfg.use_macd_filter))
    cfg.macd_fast = int(strat.get("macd_fast", cfg.macd_fast))
    cfg.macd_slow = int(strat.get("macd_slow", cfg.macd_slow))
    cfg.macd_signal = int(strat.get("macd_signal", cfg.macd_signal))
    cfg.min_cloud_thickness = float(strat.get("min_cloud_thickness", cfg.min_cloud_thickness))
    cfg.exit_mode = str(strat.get("exit_mode", cfg.exit_mode)).lower()
    cfg.partial_target_pct = float(strat.get("partial_target_pct", cfg.partial_target_pct))
    cfg.partial_exit_fraction = float(strat.get("partial_exit_fraction", cfg.partial_exit_fraction))

    opts = raw.get("options", {}) or {}
    cfg.target_delta = float(opts.get("target_delta", cfg.target_delta))
    cfg.delta_min = float(opts.get("delta_min", cfg.delta_min))
    cfg.delta_max = float(opts.get("delta_max", cfg.delta_max))
    cfg.expiry = opts.get("expiry", cfg.expiry)
    cfg.min_days_to_expiry = int(opts.get("min_days_to_expiry", cfg.min_days_to_expiry))
    cfg.order_type = str(opts.get("order_type", cfg.order_type)).upper()
    cfg.limit_tolerance_pct = float(opts.get("limit_tolerance_pct", cfg.limit_tolerance_pct))
    cfg.product = opts.get("product", cfg.product)
    cfg.min_volume = int(opts.get("min_volume", cfg.min_volume))
    cfg.min_open_interest = int(opts.get("min_open_interest", cfg.min_open_interest))
    cfg.max_spread_pct = float(opts.get("max_spread_pct", cfg.max_spread_pct))

    risk = raw.get("risk", {}) or {}
    cfg.lots_per_trade = int(risk.get("lots_per_trade", cfg.lots_per_trade))
    cfg.max_trades_per_day_per_pipeline = int(
        risk.get("max_trades_per_day_per_pipeline", cfg.max_trades_per_day_per_pipeline)
    )
    cfg.max_daily_loss = float(risk.get("max_daily_loss", cfg.max_daily_loss))
    cfg.daily_profit_target = float(risk.get("daily_profit_target", cfg.daily_profit_target))

    web = raw.get("web", {}) or {}
    cfg.web_host = web.get("host", cfg.web_host)
    cfg.web_port = int(web.get("port", cfg.web_port))
    cfg.web_refresh_seconds = int(web.get("refresh_seconds", cfg.web_refresh_seconds))

    paper = raw.get("paper", {}) or {}
    cfg.paper_starting_cash = float(paper.get("starting_cash", cfg.paper_starting_cash))
    cfg.paper_slippage_pct = float(paper.get("slippage_pct", cfg.paper_slippage_pct))
    cfg.paper_state_file = paper.get("state_file", cfg.paper_state_file)
    cfg.paper_trade_log = paper.get("trade_log", cfg.paper_trade_log)

    live = raw.get("live", {}) or {}
    cfg.live_trade_log = live.get("trade_log", cfg.live_trade_log)

    cfg.instruments = [
        IndexConfig(
            name=item["name"],
            key=item["key"],
            enabled=bool(item.get("enabled", True)),
            options_available=bool(item.get("options_available", True)),
            trade_enabled=bool(item.get("trade_enabled", True)),
            min_cloud_thickness=(
                float(item["min_cloud_thickness"])
                if item.get("min_cloud_thickness") is not None
                else None
            ),
        )
        for item in (raw.get("instruments") or [])
    ]

    mom = raw.get("momentum", {}) or {}
    scr = mom.get("screening", {}) or {}
    cfg.mom_min_gap_pct = float(scr.get("min_gap_pct", cfg.mom_min_gap_pct))
    cfg.mom_min_rvol = float(scr.get("min_rvol", cfg.mom_min_rvol))
    cfg.mom_min_oi_change_pct = float(scr.get("min_oi_change_pct", cfg.mom_min_oi_change_pct))
    cfg.mom_min_depth_ratio = float(scr.get("min_depth_ratio", cfg.mom_min_depth_ratio))
    cfg.mom_opening_window_minutes = int(
        scr.get("opening_window_minutes", cfg.mom_opening_window_minutes)
    )
    filt = mom.get("filters", {}) or {}
    cfg.mom_require_confirmation = bool(filt.get("require_confirmation", cfg.mom_require_confirmation))
    cfg.mom_require_pdh_pdl = bool(filt.get("require_pdh_pdl", cfg.mom_require_pdh_pdl))
    cfg.mom_require_cvd = bool(filt.get("require_cvd", cfg.mom_require_cvd))
    ich = mom.get("ichimoku", {}) or {}
    cfg.mom_tenkan = int(ich.get("tenkan", cfg.mom_tenkan))
    cfg.mom_kijun = int(ich.get("kijun", cfg.mom_kijun))
    cfg.mom_senkou_b = int(ich.get("senkou_b", cfg.mom_senkou_b))
    cfg.mom_displacement = int(ich.get("displacement", cfg.mom_displacement))
    cfg.mom_chikou_period = int(ich.get("chikou_period", cfg.mom_chikou_period))
    cfg.mom_require_chikou = bool(ich.get("require_chikou", cfg.mom_require_chikou))
    cfg.mom_require_tenkan_kijun = bool(ich.get("require_tenkan_kijun", cfg.mom_require_tenkan_kijun))
    mopt = mom.get("options", {}) or {}
    cfg.mom_delta_min = float(mopt.get("delta_min", cfg.mom_delta_min))
    cfg.mom_delta_max = float(mopt.get("delta_max", cfg.mom_delta_max))
    cfg.mom_target_delta = float(mopt.get("target_delta", cfg.mom_target_delta))
    cfg.mom_max_risk_pct = float(mopt.get("max_risk_pct", cfg.mom_max_risk_pct))
    if mom.get("timeframes_minutes"):
        cfg.mom_timeframes_minutes = list(mom.get("timeframes_minutes"))
    uni = mom.get("universe", {}) or {}
    cfg.mom_auto_universe = bool(uni.get("auto", cfg.mom_auto_universe))
    cfg.mom_top_n = int(uni.get("top_n", cfg.mom_top_n))
    cfg.mom_universe_limit = int(uni.get("limit", cfg.mom_universe_limit))
    if uni.get("scan_times"):
        cfg.mom_scan_times = [_parse_time(t, time(9, 15)) for t in uni.get("scan_times")]
    mtrade = mom.get("trade", {}) or {}
    cfg.mom_trade_with_ichimoku = bool(mtrade.get("with_ichimoku", cfg.mom_trade_with_ichimoku))
    cfg.mom_no_index_trade = bool(mtrade.get("no_index", cfg.mom_no_index_trade))
    cfg.momentum_symbols = [
        MomentumSymbol(
            name=item["name"],
            key=item["key"],
            futures_key=item.get("futures_key"),
            enabled=bool(item.get("enabled", True)),
        )
        for item in (mom.get("symbols") or [])
    ]
    return cfg
