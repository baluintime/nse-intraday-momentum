"""Daily profit-target: mark-to-market, auto square-off, and halt."""

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

    def __init__(self, prices):
        self.prices = prices

    def ltp_single(self, k):
        return self.prices.get(k)

    def ltp(self, keys):
        return {k: self.prices[k] for k in keys if k in self.prices}


def make_engine(tmp_path, target):
    cfg = Config()
    cfg.mode = "paper"
    cfg.daily_profit_target = target
    cfg.max_trades_per_day_per_pipeline = 0
    cfg.paper_slippage_pct = 0.0
    cfg.paper_state_file = str(tmp_path / "s.json")
    cfg.paper_trade_log = str(tmp_path / "t.csv")
    cfg.instruments = [IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)]
    api = FakeAPI({"NSE_FO|1": 100.0})
    return Engine(cfg, api), api


def test_mark_to_market(tmp_path):
    engine, api = make_engine(tmp_path, target=0)
    engine.broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY 25000 CE", 75, "LONG", None, strike=25000)
    api.prices["NSE_FO|1"] = 120.0
    unreal, detail = engine.broker.mark_to_market()
    assert unreal == (120.0 - 100.0) * 75
    assert detail["NIFTY:1m"]["strike"] == 25000
    assert detail["NIFTY:1m"]["ltp"] == 120.0
    assert detail["NIFTY:1m"]["upnl"] == 1500.0


def test_target_hit_squares_off_and_halts(tmp_path):
    engine, api = make_engine(tmp_path, target=1000)
    engine.broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY 25000 CE", 75, "LONG", None, strike=25000)
    api.prices["NSE_FO|1"] = 120.0  # unrealized 1500 >= target 1000

    assert engine.check_profit_target() is True
    assert engine._halted is True
    assert "target" in engine._halt_reason.lower()
    assert engine.broker.open_positions() == []  # squared off at market
    assert engine.broker.realized_pnl_today() == 1500.0


def test_below_target_no_halt(tmp_path):
    engine, api = make_engine(tmp_path, target=5000)
    engine.broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY 25000 CE", 75, "LONG", None, strike=25000)
    api.prices["NSE_FO|1"] = 110.0  # unrealized 750 < target 5000
    assert engine.check_profit_target() is False
    assert engine._halted is False
    assert len(engine.broker.open_positions()) == 1


def test_target_zero_disables(tmp_path):
    engine, api = make_engine(tmp_path, target=0)
    engine.broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY 25000 CE", 75, "LONG", None, strike=25000)
    api.prices["NSE_FO|1"] = 500.0  # huge profit, but target disabled
    assert engine.check_profit_target() is False
    assert engine._halted is False


def test_halt_blocks_new_entries(tmp_path):
    engine, api = make_engine(tmp_path, target=1000)
    engine._now = lambda: datetime(2026, 7, 20, 11, 0, tzinfo=IST)
    engine.selector.select_itm = lambda k, d, s: OptionSelection(
        "NSE_FO|2", "NIFTY CE", 25100, "CE", "2099-01-01", 65, 100.0, 0.7)
    engine._halted = True  # already halted
    runner = engine.runners[0]
    sig = Signal("NIFTY:1m", ENTER_LONG, Candle(datetime(2026, 7, 20, 11, 0, tzinfo=IST), 100, 101, 99, 100.5),
                 IchimokuState(1, 1, 1, 1))
    engine._execute(runner, sig)
    assert engine.broker.open_positions() == []  # no entry while halted
