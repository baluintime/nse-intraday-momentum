"""In-flight order lock: never place a duplicate order while one is working.

Reproduces the reported bug — a single long position triggering multiple sell
orders because the exit re-fired while a prior sell was still working.
"""

from nsemomentum.broker import LiveBroker, Position
from nsemomentum.config import Config


class FakeAPI:
    access_token = "x"
    has_token = True

    def __init__(self):
        self.n = 0
        self.status: dict[str, str] = {}
        self.avg: dict[str, float] = {}
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.default_status = "complete"  # new orders fill immediately unless told otherwise

    def ltp_single(self, k):
        return 100.0

    def ltp(self, keys):
        return {k: 100.0 for k in keys}

    def positions(self):
        return []

    def place_order(self, instrument_key, quantity, transaction_type, order_type="LIMIT", price=0.0, product="I", tag="nsemomentum"):
        self.n += 1
        oid = f"O{self.n}"
        self.placed.append({"id": oid, "side": transaction_type, "type": order_type, "qty": quantity})
        self.status[oid] = self.default_status
        self.avg[oid] = price or 100.0
        return oid

    def order_details(self, order_id):
        return {"status": self.status.get(order_id, "open"), "average_price": self.avg.get(order_id)}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.status[order_id] = "cancelled"


def broker(tmp_path):
    cfg = Config()
    cfg.mode = "live"
    cfg.live_trade_log = str(tmp_path / "live.csv")
    b = LiveBroker(cfg, FakeAPI())
    b.FILL_POLL_SECONDS = 2  # first order_details poll is immediate, so this stays fast
    return b


def _pos(pid="NIFTY:1m"):
    return Position(pid, "NSE_FO|1", "NIFTY CE", 65, 100.0, "t", "LONG")


def test_working_order_blocks_duplicate_sell(tmp_path):
    b = broker(tmp_path)
    b.state.positions["NIFTY:1m"] = _pos()
    # a prior sell is still working at the exchange
    b._pending["NIFTY:1m"] = "O_prev"
    b.api.status["O_prev"] = "open"

    pnl = b.exit("NIFTY:1m", price_hint=100.0)
    assert pnl is None                         # exit did not complete
    assert b.api.placed == []                  # NO duplicate sell was placed
    assert "NIFTY:1m" in b.state.positions     # position still held


def test_prior_order_filled_closes_position(tmp_path):
    b = broker(tmp_path)
    b.state.positions["NIFTY:1m"] = _pos()
    # the prior (unconfirmed) sell actually filled at the exchange @ 110
    b._pending["NIFTY:1m"] = "O_prev"
    b.api.status["O_prev"] = "complete"
    b.api.avg["O_prev"] = 110.0

    pnl = b.exit("NIFTY:1m", price_hint=100.0)
    assert pnl == (110.0 - 100.0) * 65         # closed using the real fill
    assert b.api.placed == []                  # no new order placed
    assert "NIFTY:1m" not in b.state.positions
    assert "NIFTY:1m" not in b._pending


def test_terminal_prior_order_allows_new(tmp_path):
    b = broker(tmp_path)
    b.state.positions["NIFTY:1m"] = _pos()
    b._pending["NIFTY:1m"] = "O_prev"
    b.api.status["O_prev"] = "cancelled"       # prior order dead -> safe to retry

    pnl = b.exit("NIFTY:1m", price_hint=100.0)
    assert len(b.api.placed) == 1              # a fresh sell was placed
    assert pnl is not None                     # and it filled
    assert "NIFTY:1m" not in b.state.positions


def test_normal_exit_places_one_order(tmp_path):
    b = broker(tmp_path)
    b.state.positions["NIFTY:1m"] = _pos()
    pnl = b.exit("NIFTY:1m", price_hint=100.0)
    assert len(b.api.placed) == 1 and b.api.placed[0]["side"] == "SELL"
    assert pnl is not None


def test_unknown_status_assumed_working(tmp_path):
    # if we can't read the pending order's status, never risk a duplicate
    from nsemomentum.upstox_api import UpstoxError

    b = broker(tmp_path)
    b.state.positions["NIFTY:1m"] = _pos()
    b._pending["NIFTY:1m"] = "O_prev"

    def boom(order_id):
        raise UpstoxError("network")

    b.api.order_details = boom
    pnl = b.exit("NIFTY:1m", price_hint=100.0)
    assert pnl is None and b.api.placed == []  # treated as still-working; no duplicate
