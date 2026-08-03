"""Entry-guard behaviour in the engine, especially the per-pipeline daily cap."""

from datetime import datetime, timedelta, timezone

from nsemomentum.config import Config, IndexConfig
from nsemomentum.engine import Engine
from nsemomentum.ichimoku import IchimokuState
from nsemomentum.candles import Candle
from nsemomentum.options import OptionSelection
from nsemomentum.strategy import ENTER_LONG, Signal

IST = timezone(timedelta(hours=5, minutes=30))


class FakeAPI:
    access_token = "x"
    has_token = True


def make_engine(tmp_path, cap):
    cfg = Config()
    cfg.mode = "paper"
    cfg.max_trades_per_day_per_pipeline = cap
    cfg.paper_state_file = str(tmp_path / "state.json")
    cfg.paper_trade_log = str(tmp_path / "trades.csv")
    cfg.instruments = [IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)]
    engine = Engine(cfg, FakeAPI())
    # trading window: fixed 11:00 IST so entry-cutoff/square-off don't interfere
    engine._now = lambda: datetime(2026, 7, 20, 11, 0, tzinfo=IST)

    # stub option selection + broker fills so entries "succeed" without network
    engine.selector.select_itm = lambda key, direction, spot: OptionSelection(
        instrument_key="NSE_FO|1", trading_symbol="NIFTY CE", strike=25000, option_type="CE",
        expiry="2026-07-24", lot_size=65, ltp=100.0, delta=0.7,
    )
    calls = {"n": 0}

    def fake_enter(pid, ik, sym, qty, direction, price_hint, underlying_spot=None, strike=None, lot_size=None):
        calls["n"] += 1
        return object()  # non-None => counted as a real entry

    engine.broker.enter = fake_enter
    return engine, calls


def _signal():
    candle = Candle(datetime(2026, 7, 20, 11, 0, tzinfo=IST), 100, 101, 99, 100.5)
    state = IchimokuState(1, 1, 1, 1)
    return Signal("NIFTY:1m", ENTER_LONG, candle, state)


def _fire(engine, n):
    runner = engine.runners[0]
    sig = _signal()
    for _ in range(n):
        engine._execute(runner, sig)


def test_cap_blocks_after_limit(tmp_path):
    engine, calls = make_engine(tmp_path, cap=3)
    _fire(engine, 5)
    assert calls["n"] == 3  # only 3 entries allowed, rest blocked


def test_cap_zero_means_unlimited(tmp_path):
    engine, calls = make_engine(tmp_path, cap=0)
    _fire(engine, 25)
    assert calls["n"] == 25  # no cap: every signal becomes an entry


def test_no_liquid_strike_records_skip(tmp_path):
    engine, _ = make_engine(tmp_path, cap=0)
    engine.selector.select_itm = lambda k, d, s: None  # no tradeable strike
    engine._execute(engine.runners[0], _signal())
    skips = engine.recent_skips()
    assert len(skips) == 1
    assert skips[0]["pipeline"] == "NIFTY:1m"
    assert "no liquid strike" in skips[0]["reason"]


def test_skip_cleared_after_successful_entry(tmp_path):
    engine, _ = make_engine(tmp_path, cap=0)
    engine.selector.select_itm = lambda k, d, s: None
    engine._execute(engine.runners[0], _signal())
    assert engine.recent_skips()  # recorded
    # next candle finds a strike and enters -> stale skip cleared
    engine.selector.select_itm = lambda k, d, s: OptionSelection(
        "NSE_FO|1", "NIFTY CE", 25000, "CE", "2026-07-24", 65, 100.0, 0.7)
    engine._execute(engine.runners[0], _signal())
    assert engine.recent_skips() == []
