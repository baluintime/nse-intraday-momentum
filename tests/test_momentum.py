from datetime import datetime, timedelta, timezone

from nsemomentum.candles import Candle
from nsemomentum.momentum import (
    AVOID,
    BUY_CE,
    BUY_PE,
    FAIL,
    NA,
    PASS,
    CandidateInput,
    MomentumConfig,
    classify_direction,
    cumulative_volume_delta,
    depth_imbalance,
    evaluate_candidate,
    gap_pct,
    kijun_exit_triggered,
    opening_range,
    oi_change_pct,
    pdh_pdl_hold,
    relative_volume,
    trend_state,
    two_candle_confirmation,
)

IST = timezone(timedelta(hours=5, minutes=30))
CFG = MomentumConfig()


def make_series(n, base=100.0, step=1.0, up=True, start_min=15, vol=100.0):
    """Build n 1-minute candles. up=True -> rising (bull) bodies, else falling."""
    out = []
    t0 = datetime(2026, 7, 31, 9, start_min, tzinfo=IST)
    for i in range(n):
        mid = base + (i * step if up else -i * step)
        o = mid
        c = mid + (step * 0.5 if up else -step * 0.5)
        hi = max(o, c) + 0.1
        lo = min(o, c) - 0.1
        out.append(Candle(t0 + timedelta(minutes=i), o, hi, lo, c, vol))
    return out


# --------------------------------------------------------------- metrics


def test_gap_pct():
    assert gap_pct(100.0, 102.0) == 2.0
    assert round(gap_pct(98.0, 100.0), 2) == 2.04
    assert gap_pct(0.0, 100.0) is None


def test_relative_volume():
    assert relative_volume(300.0, 100.0) == 3.0
    assert relative_volume(300.0, 0.0) is None


def test_oi_change_pct():
    assert oi_change_pct(100000, 104000) == 4.0
    assert oi_change_pct(None, 104000) is None
    assert oi_change_pct(0, 104000) is None


def test_depth_imbalance():
    assert depth_imbalance(3000, 1000) == 3.0
    assert depth_imbalance(0, 1000) is None
    assert depth_imbalance(1000, 0) is None


# ------------------------------------------------------ directional matrix


def test_classify_direction_matrix():
    assert classify_direction(5.0, 4.0, 3.0) == ("LONG BUILD-UP", BUY_CE)
    assert classify_direction(-5.0, 4.0, 3.0) == ("SHORT BUILD-UP", BUY_PE)
    assert classify_direction(5.0, -2.0, 3.0) == ("SHORT COVERING", AVOID)
    assert classify_direction(-5.0, -2.0, 3.0) == ("LONG UNWINDING", AVOID)


def test_classify_direction_below_threshold_and_unknown():
    # OI positive but below the +3% build-up threshold -> no conviction
    assert classify_direction(5.0, 1.0, 3.0) == ("NO BUILD-UP", AVOID)
    assert classify_direction(None, 4.0, 3.0) == ("UNKNOWN", AVOID)
    assert classify_direction(5.0, None, 3.0) == ("UNKNOWN", AVOID)


# ------------------------------------------------- false-breakout filters


def test_opening_range():
    candles = make_series(10, base=100, step=1)
    hi, lo = opening_range(candles, minutes=5)
    # first 5 candles (indexes 0..4): highs up to ~104.6, lows from ~99.9
    assert hi > lo
    assert opening_range([], 5) is None


def test_two_candle_confirmation_call():
    candles = make_series(4, base=100, step=1, up=True)
    confirmed, c = two_candle_confirmation(candles, want_ce=True)
    assert confirmed and c is not None
    # only the opening candle -> cannot confirm
    assert two_candle_confirmation(candles[:1], want_ce=True) == (False, None)


def test_two_candle_confirmation_put():
    candles = make_series(4, base=100, step=1, up=False)
    confirmed, c = two_candle_confirmation(candles, want_ce=False)
    assert confirmed and c is not None
    # a rising series never closes below the opening low -> no PUT confirmation
    rising = make_series(4, base=100, step=1, up=True)
    assert two_candle_confirmation(rising, want_ce=False) == (False, None)


def test_pdh_pdl_hold():
    candles = make_series(6, base=100, step=1, up=True)
    # PDH well below the series -> broken and held
    status, _ = pdh_pdl_hold(candles, pdh=99.0, pdl=None, want_ce=True)
    assert status == PASS
    # PDH above the whole series -> never broken
    status, _ = pdh_pdl_hold(candles, pdh=200.0, pdl=None, want_ce=True)
    assert status == FAIL
    # unavailable level
    assert pdh_pdl_hold(candles, pdh=None, pdl=None, want_ce=True)[0] == NA


def test_pdh_pdl_reentry_is_liquidity_grab():
    # break above PDH then close back inside the prior range -> FAIL
    t0 = datetime(2026, 7, 31, 9, 15, tzinfo=IST)
    candles = [
        Candle(t0, 100, 101, 99, 100.6, 100),                 # inside
        Candle(t0 + timedelta(minutes=1), 100.6, 102, 100, 101.5, 100),  # break > PDH 101
        Candle(t0 + timedelta(minutes=2), 101.5, 101.6, 100, 100.4, 100),  # back inside
    ]
    status, detail = pdh_pdl_hold(candles, pdh=101.0, pdl=None, want_ce=True)
    assert status == FAIL and "back inside" in detail


def test_cumulative_volume_delta_sign():
    assert cumulative_volume_delta(make_series(5, up=True)) > 0
    assert cumulative_volume_delta(make_series(5, up=False)) < 0


# ------------------------------------------------------ Ichimoku trend


def test_trend_state_not_ready_then_ready():
    assert trend_state([1], [1], [1], CFG).ready is False
    s = make_series(90, base=100, step=1, up=True)
    ts = trend_state([c.high for c in s], [c.low for c in s], [c.close for c in s], CFG)
    assert ts.ready
    assert ts.above_cloud and ts.tenkan_gt_kijun and ts.price_above_kijun
    assert ts.chikou_ok_long and not ts.chikou_ok_short


def test_kijun_exit_triggered():
    rising = make_series(90, base=100, step=1, up=True)
    h = [c.high for c in rising]; low = [c.low for c in rising]; c = [c.close for c in rising]
    # a long (Call) with price above Kijun -> no exit
    assert kijun_exit_triggered(h, low, c, CFG, want_ce=True) is False
    # for a Put (want_ce False) the same above-Kijun close is the opposite side -> exit
    assert kijun_exit_triggered(h, low, c, CFG, want_ce=True) != \
        kijun_exit_triggered(h, low, c, CFG, want_ce=False)


# --------------------------------------------------- full evaluation


def _bull_input(tf="1m"):
    series = make_series(90, base=100, step=1, up=True)
    return CandidateInput(
        symbol="TEST",
        timeframe=tf,
        today_candles=series,
        trend_candles=series,
        prev_close=98.0,                # first open 100 -> +2.04% gap
        prev_day_high=99.0,             # broken and held
        prev_day_low=95.0,
        opening_window_volume=300.0,
        avg_opening_window_volume=100.0,  # RVOL 3.0
        futures_oi_prev=100000.0,
        futures_oi_now=104000.0,        # +4% ΔOI
        total_buy_qty=3000.0,
        total_sell_qty=1000.0,          # depth 3:1
    )


def test_evaluate_full_bull_is_trade_ready():
    ev = evaluate_candidate(_bull_input(), CFG)
    assert ev.ready
    assert ev.action == BUY_CE
    assert ev.classification == "LONG BUILD-UP"
    assert ev.trade_ready is True
    assert ev.screen_passed is True
    # no failing checks
    assert all(c.status != FAIL for c in ev.checks)
    # every BRD layer represented
    names = {c.name for c in ev.checks}
    assert {"gap", "rvol", "oi", "depth", "direction", "kumo",
            "confirmation", "pdh_pdl", "cvd", "kijun_hold"} <= names


def test_evaluate_runs_for_both_timeframes():
    for tf in ("1m", "5m"):
        ev = evaluate_candidate(_bull_input(tf), CFG)
        assert ev.timeframe == tf and ev.trade_ready


def test_evaluate_short_covering_is_avoid():
    inp = _bull_input()
    inp.futures_oi_now = 99000.0  # ΔOI negative while price is up -> short covering
    ev = evaluate_candidate(inp, CFG)
    assert ev.action == AVOID
    assert ev.classification == "SHORT COVERING"
    assert ev.trade_ready is False
    # false-breakout filters are not run when the matrix says avoid
    assert ev.check("confirmation") is None


def test_evaluate_blocks_on_failed_gap():
    inp = _bull_input()
    inp.prev_close = 99.99  # gap ~0.01% -> fails the 1.5% gate
    ev = evaluate_candidate(inp, CFG)
    assert ev.check("gap").status == FAIL
    assert ev.trade_ready is False
    assert ev.screen_passed is False


def test_evaluate_missing_oi_is_na_not_fail():
    inp = _bull_input()
    inp.futures_oi_prev = None
    inp.futures_oi_now = None
    ev = evaluate_candidate(inp, CFG)
    # ΔOI unavailable -> matrix cannot confirm -> AVOID, but the OI check is NA not FAIL
    assert ev.check("oi").status == NA
    assert ev.action == AVOID
