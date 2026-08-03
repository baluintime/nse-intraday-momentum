"""The Ichimoku engine trading the momentum scanner's locked top-N picks."""

from nsemomentum.config import Config, IndexConfig, MomentumSymbol
from nsemomentum.engine import Engine


class StubAPI:
    has_token = True
    access_token = "t"

    def historical_candles(self, *a, **k):
        return []

    def intraday_candles(self, *a, **k):
        return []

    def positions(self):
        return []


class FakeBroker:
    """Minimal broker: only the calls the runner-sync path touches."""

    def __init__(self):
        self.open: set[str] = set()

    def position(self, pid):
        return object() if pid in self.open else None

    def seed_from_upstox(self):
        return 0


class FakeMomentum:
    top_n = 3

    def __init__(self, picks):
        self._picks = picks

    def locked_symbols(self):
        return list(self._picks)


def _sym(name):
    return MomentumSymbol(name=name, key=f"NSE_EQ|{name}", futures_key=f"NSE_FO|{name}F")


def _engine(tmp_path, picks, instruments=None, no_index=True):
    cfg = Config()
    cfg.paper_state_file = str(tmp_path / "s.json")
    cfg.paper_trade_log = str(tmp_path / "t.csv")
    cfg.mom_trade_with_ichimoku = True
    cfg.mom_no_index_trade = no_index
    cfg.instruments = instruments or []
    eng = Engine(cfg, StubAPI(), momentum=FakeMomentum(picks))
    eng.broker = FakeBroker()
    return eng


def test_no_index_means_no_index_runners(tmp_path):
    idx = [IndexConfig(name="NIFTY", key="NSE_INDEX|Nifty 50")]
    eng = _engine(tmp_path, picks=[], instruments=idx, no_index=True)
    assert eng.runners == []  # indices are not traded


def test_index_runners_kept_when_no_index_false(tmp_path):
    idx = [IndexConfig(name="NIFTY", key="NSE_INDEX|Nifty 50")]
    eng = _engine(tmp_path, picks=[], instruments=idx, no_index=False)
    assert {r.index.name for r in eng.runners} == {"NIFTY"}


def test_picks_are_added_and_warmed(tmp_path):
    eng = _engine(tmp_path, picks=[_sym("AAA"), _sym("BBB")])
    eng.sync_momentum_runners()
    assert {r.index.name for r in eng.runners} == {"AAA", "BBB"}
    assert all(r.momentum_sourced and r.warmed for r in eng.runners)
    # a stock runner trades the equity underlying via the Ichimoku pipeline
    assert eng._runner_by_name["AAA"].index.key == "NSE_EQ|AAA"
    assert eng._runner_by_name["AAA"].index.options_available is True


def test_dropped_pick_with_position_is_held_exit_only(tmp_path):
    eng = _engine(tmp_path, picks=[_sym("AAA"), _sym("BBB")])
    eng.sync_momentum_runners()
    eng.broker.open = {"BBB:1m"}          # BBB now holds a position
    eng.momentum._picks = [_sym("AAA")]   # 1PM re-scan drops BBB
    eng.sync_momentum_runners()
    assert {r.index.name for r in eng.runners} == {"AAA", "BBB"}  # BBB retained
    assert eng._runner_by_name["BBB"].index.trade_enabled is False  # no new entries
    assert eng._runner_by_name["AAA"].index.trade_enabled is True


def test_dropped_pick_without_position_is_removed(tmp_path):
    eng = _engine(tmp_path, picks=[_sym("AAA"), _sym("BBB")])
    eng.sync_momentum_runners()
    eng.momentum._picks = [_sym("AAA")]   # BBB drops, holds nothing
    eng.sync_momentum_runners()
    assert {r.index.name for r in eng.runners} == {"AAA"}


def test_reentered_pick_is_re_enabled(tmp_path):
    eng = _engine(tmp_path, picks=[_sym("AAA"), _sym("BBB")])
    eng.sync_momentum_runners()
    eng.broker.open = {"BBB:1m"}
    eng.momentum._picks = [_sym("AAA")]
    eng.sync_momentum_runners()
    assert eng._runner_by_name["BBB"].index.trade_enabled is False
    eng.momentum._picks = [_sym("AAA"), _sym("BBB")]  # BBB back in the top-N
    eng.sync_momentum_runners()
    assert eng._runner_by_name["BBB"].index.trade_enabled is True
