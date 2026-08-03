"""NSE Intraday Momentum Option Strategy — analytical core.

Implements the screening + signal methodology from the BRD
``NSE Intraday Option Trade Selection & Execution System`` (REQ-NSE-OPT-2026-V1)
as small, pure, testable functions. Nothing here touches the network; the web
layer (:mod:`nsemomentum.momentum_web`) feeds it live Upstox data.

The BRD stacks five quantitative layers, all of which must agree before a trade
is taken:

1. **Pre-open screening** (09:08–09:20): absolute gap %, relative volume (RVOL),
   near-month futures open-interest build-up (ΔOI %), and bid/ask depth
   imbalance.
2. **Directional matrix**: price direction paired with futures ΔOI classifies
   the move as LONG BUILD-UP (buy ITM Call), SHORT BUILD-UP (buy ITM Put),
   SHORT COVERING or LONG UNWINDING (both avoided).
3. **False-breakout filters**: the 2-candle confirmation rule, Previous Day
   High/Low (PDH/PDL) retest-and-hold, and Cumulative Volume Delta (CVD).
4. **Ichimoku Kumo trend retention**: price outside the cloud, Tenkan/Kijun
   alignment, Chikou span clear, and the Kijun-sen trend-hold — evaluated here
   on **both the 1-minute and 5-minute timeframes**.
5. **Exit protocol**: trail the Kijun-sen; a candle closing on the opposite side
   of the Kijun-sen ends the trade (or the 15:15 hard stop).

Every check reports ``pass`` / ``fail`` / ``na`` with a human-readable detail so
the dashboard can show exactly why a candidate did or didn't qualify. Inputs
that Upstox can't reliably supply intraday (e.g. futures ΔOI, five-level depth)
are optional — a missing input yields ``na`` and, for a gate, blocks the trade
rather than silently passing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .candles import Candle
from .ichimoku import IchimokuParams, compute_state

# ------------------------------------------------------------------ actions

BUY_CE = "BUY_CE"          # buy an ITM/ATM Call
BUY_PE = "BUY_PE"          # buy an ITM/ATM Put
AVOID = "AVOID"            # directional matrix says stand aside

# per-check status
PASS = "pass"
FAIL = "fail"
NA = "na"                  # input unavailable — treated as blocking for a gate


@dataclass(frozen=True)
class MomentumConfig:
    """Thresholds from the BRD (all overridable from config.yaml)."""

    # Layer 1 — screening
    min_gap_pct: float = 1.5          # |Gap %| >= 1.5
    min_rvol: float = 3.0             # RVOL >= 3.0
    min_oi_change_pct: float = 3.0    # near-month futures ΔOI >= +3.0 %
    min_depth_ratio: float = 2.5      # buy/sell depth >= 2.5 (or <= 1/2.5)
    opening_window_minutes: int = 5   # the 09:15–09:20 volume/range window

    # Layer 3 — false-breakout filters (each can be turned off)
    require_confirmation: bool = True
    require_pdh_pdl: bool = True
    require_cvd: bool = True

    # Layer 4 — Ichimoku Kumo trend retention
    tenkan: int = 9
    kijun: int = 26
    senkou_b: int = 52
    displacement: int = 26
    chikou_period: int = 26
    require_chikou: bool = True
    require_tenkan_kijun: bool = True

    # Layer 5 / risk — option selection & sizing
    delta_min: float = 0.50
    delta_max: float = 0.65
    target_delta: float = 0.58
    max_risk_pct: float = 1.0         # max 1 % of capital per trade

    @property
    def ich(self) -> IchimokuParams:
        return IchimokuParams(self.tenkan, self.kijun, self.senkou_b, self.displacement)


# --------------------------------------------------------------- metrics


def gap_pct(prev_close: float, today_open: float) -> float | None:
    """Overnight gap: ``((open - prev_close) / prev_close) * 100``."""
    if not prev_close:
        return None
    return (today_open - prev_close) / prev_close * 100.0


def relative_volume(window_volume: float, avg_window_volume: float) -> float | None:
    """RVOL = today's opening-window volume / the 10-day average for that window."""
    if not avg_window_volume:
        return None
    return window_volume / avg_window_volume


def oi_change_pct(prev_oi: float | None, curr_oi: float | None) -> float | None:
    """Near-month futures open-interest change ``((cur - prev) / prev) * 100``."""
    if prev_oi in (None, 0) or curr_oi is None:
        return None
    return (curr_oi - prev_oi) / prev_oi * 100.0


def depth_imbalance(total_buy_qty: float | None, total_sell_qty: float | None) -> float | None:
    """Bid/ask order-book pressure = total buy depth / total sell depth."""
    if not total_buy_qty or not total_sell_qty:
        return None
    return total_buy_qty / total_sell_qty


# ----------------------------------------------------- directional matrix


def classify_direction(
    price_change: float | None, oi_change: float | None, min_oi_pct: float
) -> tuple[str, str]:
    """Pair price direction with futures ΔOI to determine institutional intent.

    Returns ``(classification, action)`` where action is :data:`BUY_CE`,
    :data:`BUY_PE` or :data:`AVOID`, per the BRD's directional matrix:

    ==========  ================  ================  =====================
    Price ΔP    Futures ΔOI       Classification    Implication
    ==========  ================  ================  =====================
    UP          UP (> threshold)  LONG BUILD-UP     Buy ITM Call
    DOWN        UP (> threshold)  SHORT BUILD-UP    Buy ITM Put
    UP          DOWN              SHORT COVERING    Avoid (reversal risk)
    DOWN        DOWN              LONG UNWINDING    Avoid (no momentum)
    ==========  ================  ================  =====================
    """
    if price_change is None or oi_change is None:
        return "UNKNOWN", AVOID
    oi_up = oi_change >= min_oi_pct
    oi_down = oi_change < 0
    if price_change > 0 and oi_up:
        return "LONG BUILD-UP", BUY_CE
    if price_change < 0 and oi_up:
        return "SHORT BUILD-UP", BUY_PE
    if price_change > 0 and oi_down:
        return "SHORT COVERING", AVOID
    if price_change < 0 and oi_down:
        return "LONG UNWINDING", AVOID
    # OI barely moved (between 0 and the threshold): no fresh conviction.
    return "NO BUILD-UP", AVOID


# --------------------------------------------------- false-breakout filters


def opening_range(candles: list[Candle], minutes: int) -> tuple[float, float] | None:
    """High/low of the session's opening window (first ``minutes`` of trade)."""
    if not candles:
        return None
    start = candles[0].ts
    window = [c for c in candles if (c.ts - start).total_seconds() < minutes * 60]
    if not window:
        return None
    return max(c.high for c in window), min(c.low for c in window)


def two_candle_confirmation(candles: list[Candle], want_ce: bool) -> tuple[bool, Candle | None]:
    """BRD Rule 1 — no entry on the opening candle.

    The opening (first) candle sets the reference range. Confirmation requires a
    *subsequent* candle to close beyond that candle's high (for a Call) or low
    (for a Put). Returns ``(confirmed, confirming_candle)``.
    """
    if len(candles) < 2:
        return False, None
    opening = candles[0]
    for c in candles[1:]:
        if want_ce and c.close > opening.high:
            return True, c
        if not want_ce and c.close < opening.low:
            return True, c
    return False, None


def pdh_pdl_hold(
    candles: list[Candle], pdh: float | None, pdl: float | None, want_ce: bool
) -> tuple[str, str]:
    """BRD Rule 2 — Previous Day High/Low retest & hold.

    A bullish breakout must close above the PDH and then hold above it without a
    later candle closing back inside yesterday's range (a "liquidity grab").
    Returns a ``(status, detail)`` pair. ``na`` when the reference level is
    unknown.
    """
    level = pdh if want_ce else pdl
    if level is None or not candles:
        return NA, "PDH/PDL unavailable"
    broken = False
    for c in candles:
        beyond = c.close > level if want_ce else c.close < level
        if beyond:
            broken = True
        elif broken:
            # broke out earlier, now closed back inside the prior range
            edge = "PDH" if want_ce else "PDL"
            return FAIL, f"closed back inside range after breaking {edge} {level:.2f}"
    if not broken:
        edge = "PDH" if want_ce else "PDL"
        return FAIL, f"has not closed beyond {edge} {level:.2f}"
    edge = "PDH" if want_ce else "PDL"
    return PASS, f"holding beyond {edge} {level:.2f}"


def cumulative_volume_delta(candles: list[Candle]) -> float:
    """Approximate CVD from candle bodies.

    True CVD needs tick-level trade-at-bid/ask data. As a candle-only proxy an
    up candle (close > open) contributes ``+volume`` (net buying), a down candle
    ``-volume`` (net selling); dojis contribute nothing. The sign is what the
    BRD's Rule 3 cares about, and the sign is robust to this approximation.
    """
    cvd = 0.0
    for c in candles:
        if c.close > c.open:
            cvd += c.volume
        elif c.close < c.open:
            cvd -= c.volume
    return cvd


# ---------------------------------------------------- Ichimoku Kumo trend


@dataclass(frozen=True)
class TrendState:
    ready: bool
    above_cloud: bool = False
    below_cloud: bool = False
    tenkan_gt_kijun: bool = False
    chikou_ok_long: bool = False
    chikou_ok_short: bool = False
    price_above_kijun: bool = False
    price_below_kijun: bool = False
    kijun: float | None = None
    cloud_top: float | None = None
    cloud_bottom: float | None = None
    tenkan: float | None = None


def trend_state(
    highs: list[float], lows: list[float], closes: list[float], cfg: MomentumConfig
) -> TrendState:
    """Full Ichimoku Kumo picture at the latest completed candle."""
    state = compute_state(highs, lows, cfg.ich)
    if state is None:
        return TrendState(ready=False)
    close = closes[-1]
    chikou_ok_long = chikou_ok_short = False
    if len(closes) > cfg.chikou_period:
        past = closes[-1 - cfg.chikou_period]
        chikou_ok_long = close > past
        chikou_ok_short = close < past
    return TrendState(
        ready=True,
        above_cloud=close > state.cloud_top,
        below_cloud=close < state.cloud_bottom,
        tenkan_gt_kijun=state.tenkan > state.kijun,
        chikou_ok_long=chikou_ok_long,
        chikou_ok_short=chikou_ok_short,
        price_above_kijun=close > state.kijun,
        price_below_kijun=close < state.kijun,
        kijun=state.kijun,
        cloud_top=state.cloud_top,
        cloud_bottom=state.cloud_bottom,
        tenkan=state.tenkan,
    )


def kijun_exit_triggered(
    highs: list[float], lows: list[float], closes: list[float], cfg: MomentumConfig, want_ce: bool
) -> bool:
    """BRD Rule / exit — the trade ends when a candle closes on the opposite side
    of the Kijun-sen (below it for a long/Call, above it for a short/Put)."""
    state = compute_state(highs, lows, cfg.ich)
    if state is None:
        return False
    close = closes[-1]
    return close < state.kijun if want_ce else close > state.kijun


# ----------------------------------------------------------- evaluation


@dataclass
class Check:
    name: str
    status: str          # PASS | FAIL | NA
    detail: str = ""
    value: float | None = None


@dataclass
class MomentumEvaluation:
    symbol: str
    timeframe: str                      # "1m" | "5m"
    ready: bool
    action: str = AVOID                 # BUY_CE | BUY_PE | AVOID
    classification: str = "UNKNOWN"
    checks: list[Check] = field(default_factory=list)
    screen_passed: bool = False         # layer-1 gates all green
    trade_ready: bool = False           # every required layer green -> take it
    conviction: float = 0.0             # fraction of required checks passing
    blocked_reason: str | None = None
    exit_triggered: bool = False        # a held position should be closed
    trend: TrendState | None = None

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)


@dataclass
class CandidateInput:
    """Everything the evaluator needs for one (symbol, timeframe)."""

    symbol: str
    timeframe: str
    # today's completed candles of THIS timeframe (for confirmation/PDH/CVD/range)
    today_candles: list[Candle]
    # warmup + today candles of this timeframe (for Ichimoku, needs history)
    trend_candles: list[Candle]
    prev_close: float | None = None
    prev_day_high: float | None = None
    prev_day_low: float | None = None
    # opening-window volume metrics (timeframe-independent, from 1m data)
    opening_window_volume: float | None = None
    avg_opening_window_volume: float | None = None
    # near-month futures OI
    futures_oi_prev: float | None = None
    futures_oi_now: float | None = None
    # five-level depth of the underlying
    total_buy_qty: float | None = None
    total_sell_qty: float | None = None
    # intraday price move since open (for the directional matrix); if None it is
    # derived from today's candles (last close - first open)
    price_change: float | None = None


def _status_bool(ok: bool) -> str:
    return PASS if ok else FAIL


def evaluate_candidate(inp: CandidateInput, cfg: MomentumConfig) -> MomentumEvaluation:
    """Run the full five-layer BRD methodology for one symbol/timeframe."""
    ev = MomentumEvaluation(symbol=inp.symbol, timeframe=inp.timeframe, ready=False)
    checks = ev.checks

    today = inp.today_candles
    price_change = inp.price_change
    if price_change is None and today:
        price_change = today[-1].close - today[0].open

    # ---- Layer 1: screening -------------------------------------------
    g = gap_pct(inp.prev_close, today[0].open) if (inp.prev_close and today) else None
    if g is None:
        checks.append(Check("gap", NA, "gap unavailable"))
    else:
        ok = abs(g) >= cfg.min_gap_pct
        checks.append(Check("gap", _status_bool(ok), f"gap {g:+.2f}% (need |{cfg.min_gap_pct}|)", g))

    rvol = relative_volume(inp.opening_window_volume or 0.0, inp.avg_opening_window_volume or 0.0)
    if rvol is None:
        checks.append(Check("rvol", NA, "no 10-day average volume yet"))
    else:
        ok = rvol >= cfg.min_rvol
        checks.append(Check("rvol", _status_bool(ok), f"RVOL {rvol:.2f} (need {cfg.min_rvol})", rvol))

    doi = oi_change_pct(inp.futures_oi_prev, inp.futures_oi_now)
    if doi is None:
        checks.append(Check("oi", NA, "futures ΔOI unavailable"))
    else:
        ok = doi >= cfg.min_oi_change_pct
        checks.append(Check("oi", _status_bool(ok), f"ΔOI {doi:+.2f}% (need +{cfg.min_oi_change_pct})", doi))

    imb = depth_imbalance(inp.total_buy_qty, inp.total_sell_qty)
    if imb is None:
        checks.append(Check("depth", NA, "order-book depth unavailable"))
    else:
        ok = imb >= cfg.min_depth_ratio or imb <= 1.0 / cfg.min_depth_ratio
        side = "buy" if imb >= 1 else "sell"
        checks.append(Check("depth", _status_bool(ok), f"{side} imbalance {imb:.2f} (need {cfg.min_depth_ratio}:1)", imb))

    # screening passes when no layer-1 gate is red (na is not a hard fail here,
    # but it lowers conviction and is surfaced on the card)
    ev.screen_passed = all(c.status != FAIL for c in checks if c.name in ("gap", "rvol", "oi", "depth"))

    # ---- Layer 2: directional matrix ----------------------------------
    classification, action = classify_direction(price_change, doi, cfg.min_oi_change_pct)
    ev.classification = classification
    ev.action = action
    checks.append(
        Check(
            "direction",
            PASS if action != AVOID else FAIL,
            f"{classification} → {'stand aside' if action == AVOID else action.replace('_', ' ')}",
        )
    )
    want_ce = action == BUY_CE

    # ---- Layer 4: Ichimoku Kumo trend (needs history) -----------------
    trend = trend_state(
        [c.high for c in inp.trend_candles],
        [c.low for c in inp.trend_candles],
        [c.close for c in inp.trend_candles],
        cfg,
    )
    ev.trend = trend
    if not trend.ready:
        checks.append(Check("kumo", NA, "not enough candles for the cloud yet"))
        ev.blocked_reason = "warming up Ichimoku cloud"
    else:
        ev.ready = True
        if action == AVOID:
            # still show where price sits relative to the cloud for context
            zone = "above" if trend.above_cloud else "below" if trend.below_cloud else "inside"
            checks.append(Check("kumo", NA, f"price {zone} cloud (no directional trade)"))
        else:
            cloud_ok = trend.above_cloud if want_ce else trend.below_cloud
            checks.append(
                Check("kumo", _status_bool(cloud_ok),
                      f"price {'above' if want_ce else 'below'} Kumo" if cloud_ok
                      else "price not clear of the cloud")
            )
            if cfg.require_tenkan_kijun:
                tk_ok = trend.tenkan_gt_kijun if want_ce else not trend.tenkan_gt_kijun
                checks.append(Check("tenkan_kijun", _status_bool(tk_ok),
                                    f"Tenkan {'>' if want_ce else '<'} Kijun" if tk_ok
                                    else "Tenkan/Kijun not aligned"))
            if cfg.require_chikou:
                ch_ok = trend.chikou_ok_long if want_ce else trend.chikou_ok_short
                checks.append(Check("chikou", _status_bool(ch_ok),
                                    "Chikou clear of price" if ch_ok else "Chikou blocked"))
            kj_ok = trend.price_above_kijun if want_ce else trend.price_below_kijun
            checks.append(Check("kijun_hold", _status_bool(kj_ok),
                                f"holding {'above' if want_ce else 'below'} Kijun" if kj_ok
                                else "price lost the Kijun"))

    # ---- Layer 3: false-breakout filters ------------------------------
    if action != AVOID:
        if cfg.require_confirmation:
            confirmed, conf = two_candle_confirmation(today, want_ce)
            detail = (f"closed beyond opening {'high' if want_ce else 'low'} at "
                      f"{conf.ts.strftime('%H:%M')}") if confirmed else \
                     "awaiting close beyond the opening candle"
            checks.append(Check("confirmation", _status_bool(confirmed), detail))
        if cfg.require_pdh_pdl:
            status, detail = pdh_pdl_hold(today, inp.prev_day_high, inp.prev_day_low, want_ce)
            checks.append(Check("pdh_pdl", status, detail))
        if cfg.require_cvd:
            cvd = cumulative_volume_delta(today)
            cvd_ok = cvd > 0 if want_ce else cvd < 0
            checks.append(Check("cvd", _status_bool(cvd_ok),
                                f"CVD {cvd:+,.0f} ({'buying' if cvd > 0 else 'selling'})", cvd))

    # ---- exit signal for an already-open position ---------------------
    if trend.ready and action != AVOID:
        ev.exit_triggered = kijun_exit_triggered(
            [c.high for c in inp.trend_candles],
            [c.low for c in inp.trend_candles],
            [c.close for c in inp.trend_candles],
            cfg, want_ce,
        )

    # ---- verdict ------------------------------------------------------
    required = [c for c in checks if c.status != NA]
    passed = [c for c in required if c.status == PASS]
    ev.conviction = (len(passed) / len(required)) if required else 0.0
    if action == AVOID:
        ev.trade_ready = False
        ev.blocked_reason = ev.blocked_reason or f"directional matrix: {classification}"
    else:
        reds = [c for c in checks if c.status == FAIL]
        ev.trade_ready = trend.ready and not reds
        if not ev.trade_ready and ev.blocked_reason is None:
            ev.blocked_reason = "; ".join(c.name for c in reds) or "warming up"
    return ev
