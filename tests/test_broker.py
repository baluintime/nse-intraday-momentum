from nsemomentum.broker import PaperBroker
from nsemomentum.config import Config


class FakeAPI:
    def __init__(self, prices):
        self.prices = prices

    def ltp_single(self, key):
        return self.prices.get(key)


def make_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.paper_slippage_pct = 0.0
    cfg.paper_starting_cash = 100000.0
    cfg.paper_state_file = str(tmp_path / "paper_state.json")
    cfg.paper_trade_log = str(tmp_path / "trades.csv")
    return cfg


def test_partial_exit_sells_whole_lots(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|1": 100.0})  # paper fills at live LTP
    broker = PaperBroker(cfg, api)
    # 2 lots (lot size 60 -> qty 120); +20% -> partial 50% = sell 1 lot, keep 1
    broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY CE", 120, "LONG", None, lot_size=60)
    api.prices["NSE_FO|1"] = 120.0
    pnl = broker.partial_exit("NIFTY:1m", 0.5, price_hint=120.0)
    assert pnl == (120.0 - 100.0) * 60          # sold 1 lot (60)
    pos = broker.position("NIFTY:1m")
    assert pos.qty == 60 and pos.partial_taken is True
    # second call is a no-op (already taken)
    assert broker.partial_exit("NIFTY:1m", 0.5, price_hint=120.0) is None


def test_partial_exit_single_lot_noop(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|1": 120.0})
    broker = PaperBroker(cfg, api)
    broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY CE", 65, "LONG", 100.0, lot_size=65)  # 1 lot
    assert broker.partial_exit("NIFTY:1m", 0.5, price_hint=120.0) is None
    assert broker.position("NIFTY:1m").qty == 65  # untouched


def test_exit_with_zero_entry_price_records_zero_pnl(tmp_path):
    # a position with no real entry price (e.g. a badly-adopted orphan) must not
    # fabricate a huge PnL on exit
    from nsemomentum.broker import Position

    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|9": 1433.75})
    broker = PaperBroker(cfg, api)
    broker.state.positions["X:1m"] = Position(
        "X:1m", "NSE_FO|9", "BANKNIFTY 58300 PE", 30, 0.0, "t", "SHORT")
    pnl = broker.exit("X:1m", price_hint=1433.75)
    assert pnl == 0.0  # not (1433.75 - 0) * 30 = 43012.5
    assert broker.realized_pnl_today() == 0.0


def test_trade_log_records_index_price(tmp_path):
    import csv

    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|1": 200.0})
    broker = PaperBroker(cfg, api)
    broker.enter("MIDCPNIFTY:1m", "NSE_FO|1", "MIDCP CE", 120, "LONG", None, underlying_spot=14650.0)
    api.prices["NSE_FO|1"] = 210.0
    broker.exit("MIDCPNIFTY:1m", price_hint=None, note="signal", underlying_spot=14658.0)

    with open(cfg.paper_trade_log, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert "index_price" in rows[0]
    assert rows[0]["action"] == "ENTRY" and rows[0]["index_price"] == "14650.00"
    assert rows[1]["action"].startswith("EXIT") and rows[1]["index_price"] == "14658.00"
    # entry_spot persisted on the position before exit
    assert broker.position("MIDCPNIFTY:1m") is None  # closed


def test_paper_option_roundtrip(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|123": 100.0})
    broker = PaperBroker(cfg, api)

    pos = broker.enter("NIFTY:1m", "NSE_FO|123", "NIFTY CE", 75, "LONG", None, underlying_spot=25000.0)
    assert pos is not None and pos.entry_price == 100.0
    assert pos.entry_spot == 25000.0
    assert broker.state.cash == 100000.0 - 100.0 * 75
    assert broker.position_side("NIFTY:1m") == "LONG"

    api.prices["NSE_FO|123"] = 110.0
    pnl = broker.exit("NIFTY:1m", price_hint=None)
    assert pnl == (110.0 - 100.0) * 75
    assert broker.state.cash == 100000.0 + 750.0
    assert broker.position("NIFTY:1m") is None
    assert broker.realized_pnl_today() == 750.0


def test_paper_put_short_direction_is_also_long_premium(tmp_path):
    # SHORT direction = bought ITM put: PnL is still (exit - entry) * qty
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|9": 200.0})
    broker = PaperBroker(cfg, api)
    broker.enter("BANKNIFTY:5m", "NSE_FO|9", "BANKNIFTY PE", 35, "SHORT", None)
    api.prices["NSE_FO|9"] = 260.0  # put gains as index falls
    assert broker.exit("BANKNIFTY:5m", price_hint=None) == 60.0 * 35


def test_loads_old_state_with_extra_fields(tmp_path):
    import json

    cfg = make_cfg(tmp_path)
    with open(cfg.paper_state_file, "w") as fh:
        json.dump({"cash": 5000.0, "positions": {"A:1m": {
            "pipeline_id": "A:1m", "instrument_key": "NSE_FO|1", "symbol": "X",
            "qty": 10, "entry_price": 5.0, "entry_time": "t", "direction": "LONG",
            "kind": "OPTION",  # legacy field no longer on Position
        }}}, fh)
    broker = PaperBroker(cfg, FakeAPI({}))
    assert broker.position("A:1m").qty == 10


def test_paper_state_persists(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|123": 50.0})
    broker = PaperBroker(cfg, api)
    broker.enter("NIFTY:5m", "NSE_FO|123", "NIFTY CE", 75, "LONG", None)

    resumed = PaperBroker(cfg, api)
    pos = resumed.position("NIFTY:5m")
    assert pos is not None and pos.qty == 75 and pos.entry_price == 50.0


def test_paper_rejects_double_entry_and_overspend(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.paper_starting_cash = 100.0
    api = FakeAPI({"NSE_FO|123": 10.0})
    broker = PaperBroker(cfg, api)
    assert broker.enter("A:1m", "NSE_FO|123", "X", 5, "LONG", None) is not None
    assert broker.enter("A:1m", "NSE_FO|123", "X", 5, "LONG", None) is None  # already holding
    assert broker.enter("B:1m", "NSE_FO|123", "X", 50, "LONG", None) is None  # 500 > cash left
