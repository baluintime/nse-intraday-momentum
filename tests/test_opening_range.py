"""Opening-range breakout gate on IndexRunner + the engine entry guard."""

from datetime import datetime, timedelta, timezone

from nsemomentum.config import Config, IndexConfig
from nsemomentum.engine import Engine, IndexRunner
from nsemomentum.ichimoku import IchimokuParams, IchimokuState
from nsemomentum.candles import Candle
from nsemomentum.options import OptionSelection
from nsemomentum.strategy import ENTER_LONG, Signal

IST = timezone(timedelta(hours=5, minutes=30))
PARAMS = IchimokuParams()


class FakeAPI:
    access_token = "x"
    has_token = True


def runner_with_or(minutes=15):
    cfg = Config()
    cfg.opening_range_minutes = minutes
    cfg.market_open = datetime(2026, 7, 20, 9, 15).time()
    ix = IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)
    return IndexRunner(ix, cfg, PARAMS)


def candle(h, m, o, high, low, c):
    return Candle(datetime(2026, 7, 20, h, m, tzinfo=IST), o, high, low, c)


def test_range_forms_and_locks_during_window():
    r = runner_with_or(15)
    # 09:15..09:29 build the opening range: high 100.5, low 99.0
    for m in range(15, 30):
        r.update_opening_range(candle(9, m, 100, 100.5, 99.0, 100.0))
    assert r.or_high == 100.5 and r.or_low == 99.0
    assert r.or_unlocked is False  # still locked, no breakout yet


def test_unlocks_on_upside_breakout():
    r = runner_with_or(15)
    for m in range(15, 30):
        r.update_opening_range(candle(9, m, 100, 100.5, 99.0, 100.0))
    # 09:30 closes above OR-high -> unlock
    r.update_opening_range(candle(9, 30, 100.5, 101.2, 100.4, 101.0))
    assert r.or_unlocked is True


def test_unlocks_on_downside_breakout():
    r = runner_with_or(15)
    for m in range(15, 30):
        r.update_opening_range(candle(9, m, 100, 100.5, 99.0, 100.0))
    r.update_opening_range(candle(9, 30, 100, 100.1, 98.5, 98.7))  # close below OR-low
    assert r.or_unlocked is True


def test_stays_locked_inside_range_after_window():
    r = runner_with_or(15)
    for m in range(15, 30):
        r.update_opening_range(candle(9, m, 100, 100.5, 99.0, 100.0))
    for m in range(30, 45):  # chops inside the range for another 15 min
        r.update_opening_range(candle(9, m, 100, 100.4, 99.2, 100.0))
    assert r.or_unlocked is False


def test_disabled_never_locks():
    r = runner_with_or(0)
    r.update_opening_range(candle(9, 16, 100, 100.5, 99.0, 100.0))
    assert r.or_unlocked is False  # field stays default; gate is skipped in engine


def test_new_day_resets():
    r = runner_with_or(15)
    for m in range(15, 30):
        r.update_opening_range(candle(9, m, 100, 100.5, 99.0, 100.0))
    r.update_opening_range(candle(9, 30, 100.5, 101.2, 100.4, 101.0))
    assert r.or_unlocked is True
    # next day, first candle -> reset
    r.update_opening_range(Candle(datetime(2026, 7, 21, 9, 15, tzinfo=IST), 200, 200, 199, 199.5))
    assert r.or_unlocked is False and r.or_high == 200


# ---- engine gate integration ----

def make_engine(tmp_path, or_minutes):
    cfg = Config()
    cfg.mode = "paper"
    cfg.opening_range_minutes = or_minutes
    cfg.max_trades_per_day_per_pipeline = 0
    cfg.paper_state_file = str(tmp_path / "s.json")
    cfg.paper_trade_log = str(tmp_path / "t.csv")
    cfg.instruments = [IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)]
    engine = Engine(cfg, FakeAPI())
    engine._now = lambda: datetime(2026, 7, 20, 11, 0, tzinfo=IST)
    engine.selector.select_itm = lambda k, d, s: OptionSelection(
        "NSE_FO|1", "NIFTY CE", 25000, "CE", "2026-07-24", 65, 100.0, 0.7)
    calls = {"n": 0}
    engine.broker.enter = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or object())
    return engine, calls


def _signal(action=ENTER_LONG):
    c = candle(11, 0, 100, 101, 99, 100.5)
    return Signal("NIFTY:1m", action, c, IchimokuState(1, 1, 1, 1))


def test_engine_blocks_entry_until_unlocked(tmp_path):
    engine, calls = make_engine(tmp_path, or_minutes=15)
    runner = engine.runners[0]
    # locked: entry blocked
    engine._execute(runner, _signal())
    assert calls["n"] == 0
    # unlock, then entry allowed
    runner.or_unlocked = True
    engine._execute(runner, _signal())
    assert calls["n"] == 1


def test_engine_trades_from_open_when_disabled(tmp_path):
    engine, calls = make_engine(tmp_path, or_minutes=0)
    engine._execute(engine.runners[0], _signal())
    assert calls["n"] == 1  # no gate
