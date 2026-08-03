"""Optimization layer: MACD/Chikou/thickness entry filters, tiered Kijun exit,
and partial profit-taking."""

from nsemomentum.ichimoku import IchimokuParams, IchimokuState, macd_hist
from nsemomentum.strategy import (
    ENTER_LONG, ENTER_SHORT, EXIT, Eval, StrategyConfig, decide_eval, evaluate,
)


def rising(n, start=100.0):
    # convex (accelerating) uptrend so MACD momentum is clearly positive
    closes = [start + i + i * i * 0.05 for i in range(n)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    return highs, lows, closes


P = IchimokuParams(tenkan=3, kijun=9, senkou_b=12, displacement=3)


def test_macd_hist_sign():
    up = [float(i * i) for i in range(1, 60)]        # accelerating up -> hist > 0
    down = [1000.0 - i * i for i in range(1, 60)]    # accelerating down -> hist < 0
    assert macd_hist(up) > 0
    assert macd_hist(down) < 0
    assert macd_hist([1.0, 2.0]) is None  # not enough data


def test_all_filters_pass_in_strong_uptrend():
    highs, lows, closes = rising(40)
    sc = StrategyConfig(ich=P, use_macd=True, use_chikou=True, chikou_period=5)
    ev = evaluate(highs, lows, closes, sc)
    assert ev is not None and ev.signal == "LONG"
    assert ev.macd_hist is not None and ev.macd_hist > 0


def test_thickness_filter_blocks_entry():
    highs, lows, closes = rising(40)
    sc = StrategyConfig(ich=P, min_cloud_thickness=1e9)  # impossibly thick requirement
    ev = evaluate(highs, lows, closes, sc)
    assert ev.long_ok is False and ev.signal == "NEUTRAL"
    assert ev.thickness_ok is False


def test_chikou_insufficient_history_blocks():
    highs, lows, closes = rising(20)
    sc = StrategyConfig(ich=P, use_chikou=True, chikou_period=999)  # no history that far back
    ev = evaluate(highs, lows, closes, sc)
    assert ev.long_ok is False


def test_kijun_exit_ignores_tenkan_only_breach():
    highs, lows, closes = rising(40)
    sc_any = StrategyConfig(ich=P, exit_mode="any_level")
    sc_kij = StrategyConfig(ich=P, exit_mode="kijun")
    ev0 = evaluate(highs, lows, closes, sc_kij)
    mid = (ev0.state.tenkan + ev0.state.kijun) / 2  # between kijun and tenkan
    closes2 = closes[:-1] + [mid]
    ev_any = evaluate(highs, lows, closes2, sc_any)
    ev_kij = evaluate(highs, lows, closes2, sc_kij)
    assert ev_any.long_should_exit is True    # any-level: below tenkan -> exit
    assert ev_kij.long_should_exit is False   # kijun mode: still above kijun -> hold


def _eval(**kw):
    base = dict(
        ready=True, close=100.0, state=IchimokuState(1, 1, 1, 1), macd_hist=None,
        chikou_ok_long=True, chikou_ok_short=True, thickness=1.0, thickness_ok=True,
        long_ok=False, short_ok=False, long_should_exit=False, short_should_exit=False,
        signal="NEUTRAL",
    )
    base.update(kw)
    return Eval(**base)


def test_decide_eval_entry_exit_reversal():
    assert decide_eval(_eval(long_ok=True), None) == [ENTER_LONG]
    assert decide_eval(_eval(short_ok=True), None) == [ENTER_SHORT]
    assert decide_eval(_eval(long_should_exit=True), "LONG") == [EXIT]
    assert decide_eval(_eval(long_should_exit=False), "LONG") == []
    # reversal: exit long and immediately enter short in one candle
    assert decide_eval(_eval(long_should_exit=True, short_ok=True), "LONG") == [EXIT, ENTER_SHORT]


def test_build_strategy_config_per_index_thickness_override():
    from nsemomentum.config import Config, IndexConfig
    from nsemomentum.strategy import build_strategy_config

    cfg = Config()
    cfg.min_cloud_thickness = 5.0

    # no per-index value -> inherit the global
    inherit = IndexConfig(name="MIDCPNIFTY", key="k")
    assert build_strategy_config(cfg, inherit).min_cloud_thickness == 5.0

    # per-index value wins
    override = IndexConfig(name="BANKNIFTY", key="k2", min_cloud_thickness=25.0)
    assert build_strategy_config(cfg, override).min_cloud_thickness == 25.0

    # a per-index 0 is a real override (disables the gate for that index)
    off = IndexConfig(name="NIFTY", key="k3", min_cloud_thickness=0.0)
    assert build_strategy_config(cfg, off).min_cloud_thickness == 0.0

    # no index at all -> global
    assert build_strategy_config(cfg).min_cloud_thickness == 5.0


def test_config_yaml_parses_per_index_thickness(tmp_path):
    from nsemomentum.config import load_config

    yml = tmp_path / "c.yaml"
    yml.write_text(
        "strategy:\n"
        "  min_cloud_thickness: 5\n"
        "instruments:\n"
        "  - name: BANKNIFTY\n"
        "    key: 'NSE_INDEX|Nifty Bank'\n"
        "    min_cloud_thickness: 25\n"
        "  - name: MIDCPNIFTY\n"
        "    key: 'NSE_INDEX|NIFTY MID SELECT'\n"
    )
    cfg = load_config(str(yml))
    by_name = {i.name: i for i in cfg.instruments}
    assert by_name["BANKNIFTY"].min_cloud_thickness == 25.0
    assert by_name["MIDCPNIFTY"].min_cloud_thickness is None  # inherits global


def test_per_index_thickness_changes_signal():
    # same series, two indices: a thick-gate index blocks the entry the
    # loose-gate index takes
    highs, lows, closes = rising(40)
    ev0 = evaluate(highs, lows, closes, StrategyConfig(ich=P))
    thick = ev0.thickness

    loose = StrategyConfig(ich=P, min_cloud_thickness=thick / 2)
    strict = StrategyConfig(ich=P, min_cloud_thickness=thick * 2)
    assert evaluate(highs, lows, closes, loose).signal == "LONG"
    assert evaluate(highs, lows, closes, strict).signal == "NEUTRAL"
