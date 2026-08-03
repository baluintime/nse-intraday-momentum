"""Per-index, per-timeframe signal pipeline.

Each (index, timeframe) pair is an independent pipeline, per the spec's
"two completely separate, parallel execution pipelines" requirement. A
pipeline turns completed candles into abstract actions; execution (option
selection, orders, PnL) is the engine/broker's job.

The base rule is the strict Ichimoku breakout (close beyond all four lines).
The optimization layer adds optional entry filters (Chikou span, MACD
histogram, minimum cloud thickness) and a tiered exit (soft trailing stop on
the Kijun close + hard stop at the opposite Kumo edge). All extras are off by
default, so with a bare config the behaviour is the original strategy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .candles import Candle, CandleSeries
from .ichimoku import (
    IchimokuParams,
    IchimokuState,
    compute_state,
    long_entry,
    long_exit,
    macd_hist,
    short_entry,
    short_exit,
)

log = logging.getLogger(__name__)

# Actions a pipeline can emit on a candle close
EXIT = "EXIT"
ENTER_LONG = "ENTER_LONG"    # -> buy ITM CALL at next candle open
ENTER_SHORT = "ENTER_SHORT"  # -> buy ITM PUT at next candle open


@dataclass(frozen=True)
class StrategyConfig:
    ich: IchimokuParams
    use_chikou: bool = False
    chikou_period: int = 26
    use_macd: bool = False
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    min_cloud_thickness: float = 0.0
    exit_mode: str = "any_level"  # 'any_level' (original) | 'kijun' (tiered)

    @property
    def min_candles(self) -> int:
        need = self.ich.min_candles
        if self.use_macd:
            need = max(need, self.macd_slow + self.macd_signal)
        if self.use_chikou:
            need = max(need, self.chikou_period + 1)
        return need


def build_strategy_config(cfg, index=None) -> StrategyConfig:
    """Build a StrategyConfig from the app Config (duck-typed).

    When `index` (an IndexConfig) is given and it carries its own
    `min_cloud_thickness`, that per-index value overrides the global one — each
    index moves on a different point scale, so the thickness gate is tuned per
    index. All other fields stay global.
    """
    thickness = cfg.min_cloud_thickness
    if index is not None and getattr(index, "min_cloud_thickness", None) is not None:
        thickness = index.min_cloud_thickness
    return StrategyConfig(
        ich=IchimokuParams(cfg.tenkan, cfg.kijun, cfg.senkou_b, cfg.displacement),
        use_chikou=cfg.use_chikou_filter,
        chikou_period=cfg.chikou_period,
        use_macd=cfg.use_macd_filter,
        macd_fast=cfg.macd_fast,
        macd_slow=cfg.macd_slow,
        macd_signal=cfg.macd_signal,
        min_cloud_thickness=thickness,
        exit_mode=cfg.exit_mode,
    )


@dataclass(frozen=True)
class Eval:
    ready: bool
    close: float
    state: IchimokuState | None
    macd_hist: float | None
    chikou_ok_long: bool
    chikou_ok_short: bool
    thickness: float | None
    thickness_ok: bool
    long_ok: bool           # base breakout + all entry filters
    short_ok: bool
    long_should_exit: bool
    short_should_exit: bool
    signal: str             # "LONG" | "SHORT" | "NEUTRAL"


def evaluate(
    highs: list[float], lows: list[float], closes: list[float],
    sc: StrategyConfig, index: int | None = None,
) -> Eval | None:
    """Full indicator + rule evaluation at candle `index` (default: last).
    Returns None until there's enough history for the Ichimoku cloud."""
    i = len(closes) - 1 if index is None else index
    state = compute_state(highs, lows, sc.ich, index)
    if state is None:
        return None
    close = closes[i]

    # MACD histogram confluence
    hist = macd_hist(closes[: i + 1], sc.macd_fast, sc.macd_slow, sc.macd_signal) if sc.use_macd else None
    macd_ok_long = (not sc.use_macd) or (hist is not None and hist > 0)
    macd_ok_short = (not sc.use_macd) or (hist is not None and hist < 0)

    # Chikou span: current close vs the close `chikou_period` candles ago
    chikou_ok_long = chikou_ok_short = True
    if sc.use_chikou:
        if i - sc.chikou_period >= 0:
            past = closes[i - sc.chikou_period]
            chikou_ok_long = close > past
            chikou_ok_short = close < past
        else:
            chikou_ok_long = chikou_ok_short = False  # not enough history -> block

    # Minimum cloud thickness (low-volatility consolidation filter)
    thickness = abs(state.span_a - state.span_b)
    thickness_ok = sc.min_cloud_thickness <= 0 or thickness >= sc.min_cloud_thickness

    base_long = long_entry(close, state)
    base_short = short_entry(close, state)
    long_ok = base_long and macd_ok_long and chikou_ok_long and thickness_ok
    short_ok = base_short and macd_ok_short and chikou_ok_short and thickness_ok

    if sc.exit_mode == "kijun":
        # soft trailing stop on the Kijun close + hard stop at the opposite Kumo edge
        long_should_exit = close < state.kijun or close < state.cloud_bottom
        short_should_exit = close > state.kijun or close > state.cloud_top
    else:
        long_should_exit = long_exit(close, state)
        short_should_exit = short_exit(close, state)

    signal = "LONG" if long_ok else "SHORT" if short_ok else "NEUTRAL"
    return Eval(
        ready=True, close=close, state=state, macd_hist=hist,
        chikou_ok_long=chikou_ok_long, chikou_ok_short=chikou_ok_short,
        thickness=thickness, thickness_ok=thickness_ok,
        long_ok=long_ok, short_ok=short_ok,
        long_should_exit=long_should_exit, short_should_exit=short_should_exit,
        signal=signal,
    )


def decide_eval(ev: Eval, position_side: str | None) -> list[str]:
    """Turn an Eval + held side into ordered actions (EXIT before a reversal)."""
    actions: list[str] = []
    side = position_side
    if side == "LONG" and ev.long_should_exit:
        actions.append(EXIT)
        side = None
    elif side == "SHORT" and ev.short_should_exit:
        actions.append(EXIT)
        side = None
    if side is None:
        if ev.long_ok:
            actions.append(ENTER_LONG)
        elif ev.short_ok:
            actions.append(ENTER_SHORT)
    return actions


def decide(close: float, state: IchimokuState, position_side: str | None) -> list[str]:
    """Original close-vs-all-levels decision (no filters, any-level exit).
    Retained for the backtester and as the base-rule reference."""
    actions: list[str] = []
    side = position_side
    if side == "LONG" and long_exit(close, state):
        actions.append(EXIT)
        side = None
    elif side == "SHORT" and short_exit(close, state):
        actions.append(EXIT)
        side = None
    if side is None:
        if long_entry(close, state):
            actions.append(ENTER_LONG)
        elif short_entry(close, state):
            actions.append(ENTER_SHORT)
    return actions


@dataclass
class Signal:
    pipeline_id: str
    action: str
    candle: Candle
    state: IchimokuState


class Pipeline:
    def __init__(self, index_name: str, timeframe_min: int, sc: StrategyConfig):
        self.index_name = index_name
        self.timeframe_min = timeframe_min
        self.sc = sc
        self.series = CandleSeries(max_len=max(2000, sc.min_candles * 4))

    @property
    def pipeline_id(self) -> str:
        return f"{self.index_name}:{self.timeframe_min}m"

    def warmup(self, candles: list[Candle]) -> None:
        for c in candles:
            self.series.append(c)

    def evaluate(self) -> Eval | None:
        return evaluate(self.series.highs, self.series.lows, self.series.closes, self.sc)

    def on_candle_close(self, candle: Candle, position_side: str | None) -> list[Signal]:
        """Process a completed candle; returns zero or more signals."""
        if not self.series.append(candle):
            return []
        ev = self.evaluate()
        if ev is None:
            return []
        actions = decide_eval(ev, position_side)
        if actions:
            s = ev.state
            log.info(
                "%s %s close=%.2f tenkan=%.2f kijun=%.2f spanA=%.2f spanB=%.2f "
                "macd=%s thick=%.2f -> %s",
                self.pipeline_id, candle.ts.strftime("%H:%M"), candle.close,
                s.tenkan, s.kijun, s.span_a, s.span_b,
                f"{ev.macd_hist:+.2f}" if ev.macd_hist is not None else "n/a",
                ev.thickness or 0.0, ",".join(actions),
            )
        return [Signal(self.pipeline_id, a, candle, ev.state) for a in actions]
