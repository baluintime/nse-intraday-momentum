"""Live position reconciliation against Upstox: detect mismatch, seed, gate."""

from datetime import datetime, timedelta, timezone

from nsemomentum.broker import LiveBroker
from nsemomentum.config import Config, IndexConfig
from nsemomentum.engine import Engine

IST = timezone(timedelta(hours=5, minutes=30))


class FakeAPI:
    """Live-broker fake: serves Upstox positions and no-ops orders."""

    access_token = "x"
    has_token = True

    def __init__(self, upstox_positions=None):
        self._positions = upstox_positions or []

    def positions(self):
        return self._positions

    def ltp(self, keys):
        return {}

    def ltp_single(self, k):
        return None


def cfg(tmp_path):
    c = Config()
    c.mode = "live"
    c.live_trade_log = str(tmp_path / "live.csv")
    return c


def upos(token, qty, avg, symbol):
    return {"instrument_token": token, "quantity": qty, "average_price": avg, "tradingsymbol": symbol}


def test_in_sync_no_mismatch(tmp_path):
    api = FakeAPI([upos("NSE_FO|1", 65, 240.0, "NIFTY 23500 CE")])
    b = LiveBroker(cfg(tmp_path), api)
    # app holds the same
    b.state.positions["NIFTY:1m"] = _pos("NIFTY:1m", "NSE_FO|1", 65)
    assert b.reconcile() == []


def test_orphan_detected(tmp_path):
    # Upstox holds a position the app doesn't know about
    api = FakeAPI([upos("NSE_FO|9", 60, 322.5, "FINNIFTY 26050 PE")])
    b = LiveBroker(cfg(tmp_path), api)
    m = b.reconcile()
    assert len(m) == 1
    assert m[0]["symbol"] == "FINNIFTY 26050 PE"
    assert m[0]["app_qty"] == 0 and m[0]["upstox_qty"] == 60


def test_quantity_mismatch_detected(tmp_path):
    # two pipelines each bought 120 -> Upstox nets 240, app tracks only one 120
    api = FakeAPI([upos("NSE_FO|5", 240, 163.4, "MIDCPNIFTY 14150 CE")])
    b = LiveBroker(cfg(tmp_path), api)
    b.state.positions["MIDCPNIFTY:1m"] = _pos("MIDCPNIFTY:1m", "NSE_FO|5", 120)
    m = b.reconcile()
    assert m[0]["app_qty"] == 120 and m[0]["upstox_qty"] == 240


def test_seed_adopts_untracked(tmp_path):
    api = FakeAPI([upos("NSE_FO|9", 60, 322.5, "FINNIFTY 26050 PE")])
    b = LiveBroker(cfg(tmp_path), api)
    added = b.seed_from_upstox()
    assert added == 1
    pos = next(iter(b.open_positions()))
    assert pos.instrument_key == "NSE_FO|9" and pos.qty == 60 and pos.entry_price == 322.5
    # after seeding, reconcile is clean
    assert b.reconcile() == []


def test_engine_pauses_entries_on_mismatch(tmp_path):
    c = cfg(tmp_path)
    c.instruments = [IndexConfig("FINNIFTY", "NSE_INDEX|Nifty Fin Service", True, True)]
    api = FakeAPI([upos("NSE_FO|9", 60, 322.5, "FINNIFTY 26050 PE")])  # orphan
    engine = Engine(c, api)
    assert engine.reconcile_positions() is False
    assert engine._sync_ok is False
    assert engine._position_mismatch and engine._position_mismatch[0]["symbol"] == "FINNIFTY 26050 PE"


def test_paper_always_in_sync(tmp_path):
    from nsemomentum.broker import PaperBroker

    c = Config()
    c.mode = "paper"
    c.paper_state_file = str(tmp_path / "s.json")
    c.paper_trade_log = str(tmp_path / "t.csv")
    b = PaperBroker(c, None)
    assert b.reconcile() == []
    assert b.seed_from_upstox() == 0


def _pos(pid, key, qty):
    from nsemomentum.broker import Position
    return Position(pid, key, key, qty, 100.0, datetime.now(IST).isoformat(), "LONG")
