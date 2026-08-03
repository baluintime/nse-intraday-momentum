from datetime import datetime, timedelta, timezone

from nsemomentum.candles import Candle
from nsemomentum.ichimoku import IchimokuParams, IchimokuState
from nsemomentum.strategy import ENTER_LONG, ENTER_SHORT, EXIT, Pipeline, StrategyConfig, decide

IST = timezone(timedelta(hours=5, minutes=30))

STATE = IchimokuState(tenkan=100.0, kijun=99.0, span_a=98.0, span_b=97.0)


def test_decide_entries_when_flat():
    assert decide(101.0, STATE, None) == [ENTER_LONG]
    assert decide(96.0, STATE, None) == [ENTER_SHORT]
    assert decide(98.5, STATE, None) == []  # inside the levels: nothing


def test_decide_exit_on_any_level_breach():
    # long held, close drops below tenkan only -> exit, and no re-entry (not below all)
    assert decide(99.5, STATE, "LONG") == [EXIT]
    # long held, close above everything -> hold
    assert decide(101.0, STATE, "LONG") == []
    # short held, close pops above span_b only -> exit
    assert decide(97.5, STATE, "SHORT") == [EXIT]
    assert decide(96.0, STATE, "SHORT") == []


def test_decide_reversal_same_candle():
    # long held, close collapses below ALL levels -> exit AND enter short
    assert decide(96.0, STATE, "LONG") == [EXIT, ENTER_SHORT]
    # short held, close explodes above ALL levels -> exit AND enter long
    assert decide(101.0, STATE, "SHORT") == [EXIT, ENTER_LONG]


def _trending_candles(n: int, start_price: float, step: float) -> list[Candle]:
    out = []
    price = start_price
    t0 = datetime(2026, 7, 16, 9, 15, tzinfo=IST)
    for i in range(n):
        out.append(Candle(t0 + timedelta(minutes=i), price, price + 0.5, price - 0.5, price + step, 1))
        price += step
    return out


def test_pipeline_emits_long_in_uptrend():
    params = IchimokuParams(tenkan=2, kijun=3, senkou_b=4, displacement=2)
    p = Pipeline("TEST", 1, StrategyConfig(ich=params))
    candles = _trending_candles(10, 100.0, 1.0)
    p.warmup(candles[:-1])
    signals = p.on_candle_close(candles[-1], position_side=None)
    assert [s.action for s in signals] == [ENTER_LONG]
    # duplicate candle is ignored
    assert p.on_candle_close(candles[-1], position_side="LONG") == []
