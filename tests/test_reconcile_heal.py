"""Self-healing reconciliation: adopt an orphan the strategy can still hold,
square off one it can't, and drop phantoms Upstox has already closed."""

from datetime import datetime, timedelta, timezone

from nsemomentum.broker import Position
from nsemomentum.candles import Candle
from nsemomentum.config import Config, IndexConfig
from nsemomentum.engine import Engine

IST = timezone(timedelta(hours=5, minutes=30))


class FakeAPI:
    access_token = "x"
    has_token = True

    def __init__(self, positions, ltp=100.0):
        self._positions = positions
        self.placed = []
        self.n = 0
        self.status = {}
        self.avg = {}
        self._ltp = ltp

    def positions(self):
        return [p for p in self._positions if p["quantity"] != 0]

    def ltp(self, keys):
        return {k: self._ltp for k in keys if self._ltp}

    def ltp_single(self, k):
        return self._ltp

    def place_order(self, instrument_key, quantity, transaction_type, order_type="LIMIT",
                    price=0.0, product="I", tag="nsemomentum"):
        self.n += 1
        oid = f"O{self.n}"
        self.placed.append((transaction_type, quantity, instrument_key))
        if transaction_type == "SELL":  # reflect the close on the Upstox side
            for p in self._positions:
                if p["instrument_token"] == instrument_key:
                    p["quantity"] -= quantity
        self.status[oid] = "complete"
        self.avg[oid] = price or 100.0
        return oid

    def order_details(self, oid):
        return {"status": self.status.get(oid, "open"), "average_price": self.avg.get(oid)}

    def cancel_order(self, oid):
        self.status[oid] = "cancelled"


def upos(token, qty, avg, symbol):
    return {"instrument_token": token, "quantity": qty, "average_price": avg, "tradingsymbol": symbol}


def make_engine(tmp_path, positions):
    c = Config()
    c.mode = "live"
    c.live_trade_log = str(tmp_path / "live.csv")
    c.instruments = [IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)]
    e = Engine(c, FakeAPI(positions))
    e.broker.FILL_POLL_SECONDS = 2
    return e


def warm(engine, up=True):
    n = engine.params.min_candles + 6
    t0 = datetime(2026, 7, 29, 9, 15, tzinfo=IST)
    step = 1.0 if up else -1.0
    price = 100.0 if up else 400.0
    candles = []
    for i in range(n):
        candles.append(Candle(t0 + timedelta(minutes=i), price, price + 0.5, price - 0.5, price + step, 1))
        price += step
    for p in engine.runners[0].pipelines.values():
        p.warmup(candles)


def test_orphan_kept_when_signal_still_valid(tmp_path):
    e = make_engine(tmp_path, [upos("NSE_FO|1", 65, 240.0, "NIFTY 24050 CE")])
    warm(e, up=True)  # bullish -> a CE (long) is still a valid trade -> adopt & keep
    assert e.reconcile_positions() is True
    held = [p for p in e.broker.open_positions() if p.instrument_key == "NSE_FO|1"]
    assert held and held[0].qty == 65 and held[0].direction == "LONG"
    assert e.api.placed == []  # kept, nothing sold


def test_orphan_squared_when_signal_invalid(tmp_path):
    e = make_engine(tmp_path, [upos("NSE_FO|1", 65, 240.0, "NIFTY 24050 CE")])
    warm(e, up=False)  # bearish -> a CE should be exited -> square it off
    assert e.reconcile_positions() is True  # squared off -> Upstox flat -> in sync
    assert e.api.placed and e.api.placed[0][0] == "SELL" and e.api.placed[0][1] == 65
    assert e.broker.open_positions() == []


def test_phantom_dropped_when_upstox_flat(tmp_path):
    e = make_engine(tmp_path, [])  # Upstox holds nothing
    e.broker.state.positions["NIFTY:1m"] = Position(
        "NIFTY:1m", "NSE_FO|1", "NIFTY 24050 CE", 65, 240.0, "t", "LONG")
    assert e.reconcile_positions() is True  # phantom dropped -> in sync
    assert e.broker.open_positions() == []
    assert e.api.placed == []  # nothing sold (already closed on Upstox)


def test_orphan_zero_avg_adopts_at_ltp(tmp_path):
    # Upstox avg is 0 (not yet populated) -> adopt at the live LTP, never at 0
    e = make_engine(tmp_path, [upos("NSE_FO|1", 65, 0.0, "NIFTY 24050 CE")])
    e.api._ltp = 250.0
    warm(e, up=True)
    assert e.reconcile_positions() is True
    held = [p for p in e.broker.open_positions() if p.instrument_key == "NSE_FO|1"]
    assert held and held[0].entry_price == 250.0  # not 0


def test_orphan_no_price_squared_not_adopted(tmp_path):
    # avg 0 AND no LTP -> cannot price it -> square off instead of adopting at 0
    e = make_engine(tmp_path, [upos("NSE_FO|1", 65, 0.0, "NIFTY 24050 CE")], )
    e.api._ltp = 0.0
    warm(e, up=True)
    e.reconcile_positions()
    assert e.api.placed and e.api.placed[0][0] == "SELL"  # squared off, never adopted at 0
    assert e.broker.open_positions() == []


def test_recent_order_defers_heal(tmp_path):
    # if we just traded this instrument, don't act again this cycle (no double-sell)
    e = make_engine(tmp_path, [upos("NSE_FO|1", 65, 240.0, "NIFTY 24050 CE")])
    warm(e, up=False)  # bearish -> would normally square off
    import time as _t
    e.broker._recent_orders["NSE_FO|1"] = _t.time()  # we just traded it
    e.reconcile_positions()
    assert e.api.placed == []              # deferred — no square-off fired
    assert e._sync_ok is False             # stays paused one cycle until Upstox reflects


def make_stock_engine(tmp_path, positions, name="RELIANCE", key="NSE_EQ|INE002A01018"):
    """Engine whose only runner is an F&O STOCK (as in momentum-linked trading),
    so orphan reconciliation is exercised on equity options, not index options."""
    c = Config()
    c.mode = "live"
    c.live_trade_log = str(tmp_path / "live.csv")
    c.instruments = [IndexConfig(name, key, True, True)]
    e = Engine(c, FakeAPI(positions))  # momentum=None -> runners come from instruments
    e.broker.FILL_POLL_SECONDS = 2
    return e


def test_equity_call_orphan_adopted(tmp_path):
    # a RELIANCE call orphan under a bullish trend -> adopt & keep (LONG)
    e = make_stock_engine(tmp_path, [upos("NSE_FO|RILCE", 250, 55.0, "RELIANCE 3000 CE")])
    warm(e, up=True)
    assert e.reconcile_positions() is True
    held = [p for p in e.broker.open_positions() if p.instrument_key == "NSE_FO|RILCE"]
    assert held and held[0].direction == "LONG" and held[0].qty == 250
    assert e.api.placed == []  # kept, nothing sold


def test_equity_put_orphan_adopted_as_short(tmp_path):
    # a RELIANCE put orphan under a bearish trend -> adopt & keep (SHORT)
    e = make_stock_engine(tmp_path, [upos("NSE_FO|RILPE", 250, 60.0, "RELIANCE 2800 PE")])
    warm(e, up=False)
    assert e.reconcile_positions() is True
    held = [p for p in e.broker.open_positions() if p.instrument_key == "NSE_FO|RILPE"]
    assert held and held[0].direction == "SHORT"
    assert e.api.placed == []


def test_equity_option_hyphen_underlying_matches(tmp_path):
    # BAJAJ-AUTO option symbol comes back without the hyphen — must still match
    e = make_stock_engine(
        tmp_path, [upos("NSE_FO|BJ", 200, 40.0, "BAJAJAUTO 9000 CE")],
        name="BAJAJ-AUTO", key="NSE_EQ|BJA",
    )
    warm(e, up=True)
    r = e._runner_for_symbol("BAJAJAUTO 9000 CE")
    assert r is not None and r.index.name == "BAJAJ-AUTO"
    assert e.reconcile_positions() is True
    assert [p for p in e.broker.open_positions() if p.instrument_key == "NSE_FO|BJ"]  # adopted


def test_index_option_orphan_squared_when_no_index_runner(tmp_path):
    # a stock-only (momentum) engine has no index runner, so an index-option
    # orphan can't be adopted -> it is squared off
    e = make_stock_engine(tmp_path, [upos("NSE_FO|NF", 75, 120.0, "NIFTY 24050 CE")])
    warm(e, up=True)
    e.reconcile_positions()
    assert e.api.placed and e.api.placed[0][0] == "SELL"
    assert e.broker.open_positions() == []


def test_seed_labels_put_as_short(tmp_path):
    e = make_stock_engine(tmp_path, [upos("NSE_FO|RILPE", 250, 60.0, "RELIANCE 2800 PE")])
    e.broker.seed_from_upstox()
    held = [p for p in e.broker.open_positions() if p.instrument_key == "NSE_FO|RILPE"]
    assert held and held[0].direction == "SHORT"


def test_paper_mode_never_paused(tmp_path):
    c = Config()
    c.mode = "paper"
    c.paper_state_file = str(tmp_path / "s.json")
    c.paper_trade_log = str(tmp_path / "t.csv")
    c.instruments = [IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)]

    class PaperAPI:
        access_token = "x"
        has_token = True

    e = Engine(c, PaperAPI())
    assert e.reconcile_positions() is True
