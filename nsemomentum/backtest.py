"""Quick historical sanity-check of the signal logic.

Replays historical 1m candles through the same pipelines used live. PnL is
approximated at index level (entry at next candle open, exit at signal
candle close) and converted to an approximate option PnL using the target
delta — it does NOT model option premiums, theta or spreads. Use it to
inspect signal frequency and directional edge, not as a promise of returns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .candles import Candle, TimeframeAggregator
from .config import Config
from .ichimoku import IchimokuParams, compute_state
from .strategy import ENTER_LONG, ENTER_SHORT, EXIT, decide
from .tzutil import get_zone
from .upstox_api import UpstoxAPI, UpstoxError

log = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    pipeline_id: str
    direction: str
    entry_ts: datetime
    entry: float
    exit_ts: datetime
    exit: float

    @property
    def points(self) -> float:
        sign = 1 if self.direction == "LONG" else -1
        return (self.exit - self.entry) * sign


def _run_series(pipeline_id: str, candles: list[Candle], params: IchimokuParams) -> list[BacktestTrade]:
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    trades: list[BacktestTrade] = []
    side: str | None = None
    pending: str | None = None  # entry decided on prev close, fills at this candle's open
    entry_px = 0.0
    entry_ts: datetime | None = None

    for i, candle in enumerate(candles):
        if pending is not None:
            side = "LONG" if pending == ENTER_LONG else "SHORT"
            entry_px, entry_ts = candle.open, candle.ts
            pending = None
        state = compute_state(highs, lows, params, index=i)
        if state is None:
            continue
        # force day-end square off: exit if this is the last candle of the day
        last_of_day = i + 1 >= len(candles) or candles[i + 1].ts.date() != candle.ts.date()
        actions = decide(candle.close, state, side)
        if side is not None and (EXIT in actions or last_of_day):
            trades.append(
                BacktestTrade(pipeline_id, side, entry_ts, entry_px, candle.ts, candle.close)
            )
            side = None
        if not last_of_day:
            for a in actions:
                if a in (ENTER_LONG, ENTER_SHORT) and side is None:
                    pending = a
    return trades


def run_backtest(cfg: Config, api: UpstoxAPI, days: int) -> None:
    tz = get_zone(cfg.timezone)
    params = IchimokuParams(cfg.tenkan, cfg.kijun, cfg.senkou_b, cfg.displacement)
    now = datetime.now(tz)
    to_date = now.strftime("%Y-%m-%d")
    from_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"\nBacktest {from_date} -> {to_date} (index-level approximation, delta={cfg.target_delta})")
    print("=" * 96)
    header = f"{'pipeline':<18}{'trades':>7}{'wins':>6}{'losses':>8}{'points':>10}{'~option PnL/lot':>18}"
    print(header)
    print("-" * 96)

    for index in cfg.enabled_instruments:
        try:
            rows = api.historical_candles(index.key, to_date, from_date)
        except UpstoxError as exc:
            print(f"{index.name:<18} data fetch failed: {exc}")
            continue
        one_min = sorted((Candle.from_upstox(r) for r in rows), key=lambda c: c.ts)
        series_by_tf: dict[int, list[Candle]] = {}
        for tf in cfg.timeframes_minutes:
            if tf == 1:
                series_by_tf[1] = one_min
            else:
                agg = TimeframeAggregator(tf)
                out = [done for c in one_min if (done := agg.feed(c))]
                tail = agg.flush()
                if tail is not None:
                    out.append(tail)
                series_by_tf[tf] = out

        for tf, candles in series_by_tf.items():
            pid = f"{index.name}:{tf}m"
            trades = _run_series(pid, candles, params)
            points = sum(t.points for t in trades)
            wins = sum(1 for t in trades if t.points > 0)
            approx = points * cfg.target_delta
            print(
                f"{pid:<18}{len(trades):>7}{wins:>6}{len(trades) - wins:>8}"
                f"{points:>10.1f}{approx:>15.1f} pts"
            )
    print("-" * 96)
    print("~option PnL/lot = index points x target delta; multiply by lot size for INR. "
          "Premium decay and spreads are not modelled.\n")
