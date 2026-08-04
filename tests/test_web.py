from datetime import datetime, timedelta, timezone

from nsemomentum.candles import Candle
from nsemomentum.config import Config, IndexConfig
from nsemomentum.ichimoku import IchimokuParams
from nsemomentum.strategy import StrategyConfig
from nsemomentum.web import DashboardService, analyze_series, create_app

IST = timezone(timedelta(hours=5, minutes=30))
PARAMS = StrategyConfig(ich=IchimokuParams(tenkan=2, kijun=3, senkou_b=4, displacement=2))


def trending(n, start, step, t0=None):
    t0 = t0 or datetime(2026, 7, 16, 9, 15, tzinfo=IST)
    out, p = [], start
    for i in range(n):
        out.append(Candle(t0 + timedelta(minutes=i), p, p + 0.5, p - 0.5, p + step, 1))
        p += step
    return out


def test_analyze_series_uptrend_long():
    a = analyze_series(trending(12, 100.0, 1.0), PARAMS)
    assert a["ready"] is True
    assert a["signal"] == "LONG"
    assert a["zone"] == "ABOVE CLOUD"
    assert len(a["levels"]) == 4
    assert all(lv["above"] for lv in a["levels"])
    assert a["cloud"]["top"] >= a["cloud"]["bottom"]


def test_analyze_series_downtrend_short():
    a = analyze_series(trending(12, 100.0, -1.0), PARAMS)
    assert a["signal"] == "SHORT"
    assert a["zone"] == "BELOW CLOUD"
    assert all(lv["below"] for lv in a["levels"])


def test_analyze_series_warmup():
    a = analyze_series(trending(3, 100.0, 1.0), PARAMS)
    assert a["ready"] is False
    assert a["needed"] == PARAMS.min_candles
    assert analyze_series([], PARAMS) is None


class FakeAPI:
    """Serves a long uptrend split across yesterday (historical) and today (intraday)."""

    def __init__(self, token="faketoken"):
        yday = datetime(2026, 7, 15, 9, 15, tzinfo=IST)
        today = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
        self._hist = trending(100, 1000.0, 0.5, t0=yday)
        # far in the past relative to "now" so every candle counts as complete
        self._intra = trending(60, 1050.0, 1.0, t0=today - timedelta(days=0, hours=9))
        self.access_token = token

    @property
    def has_token(self):
        return bool(self.access_token)

    def set_token(self, token):
        self.access_token = token

    @staticmethod
    def _rows(candles):
        return [[c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume, 0] for c in reversed(candles)]

    def historical_candles(self, key, to_date, from_date, unit="1minute"):
        return self._rows(self._hist)

    def intraday_candles(self, key, unit="1minute"):
        return self._rows(self._intra)

    def ltp(self, keys):
        return {k: 1111.0 for k in keys}


def make_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.paper_state_file = str(tmp_path / "nope.json")
    cfg.instruments = [IndexConfig(name="FAKE", key="NSE_INDEX|Fake", options_available=False)]
    # these tests cover the plain index dashboard/engine path (the momentum-linked
    # path — dashboard shows the scanner's picks, engine trades them — has its own
    # tests in test_momentum_engine.py)
    cfg.mom_trade_with_ichimoku = False
    cfg.mom_no_index_trade = False
    return cfg


def test_dashboard_payload(tmp_path):
    svc = DashboardService(make_cfg(tmp_path), FakeAPI())
    payload = svc.payload()
    assert payload["market"]["status"] in ("OPEN", "CLOSED", "PRE-OPEN")
    assert payload["error"] is None
    (ix,) = payload["indices"]
    assert ix["name"] == "FAKE" and ix["ltp"] == 1111.0
    assert [p["timeframe"] for p in ix["pipelines"]] == ["1m", "5m"]
    one_m = ix["pipelines"][0]
    assert one_m["ready"] and one_m["signal"] == "LONG"
    assert payload["paper"] is None
    # cached within TTL: same object returned
    assert svc.payload() is payload


def test_flask_routes(tmp_path):
    app = create_app(make_cfg(tmp_path), FakeAPI())
    client = app.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert b"NSE Momentum" in page.data
    api = client.get("/api/dashboard")
    assert api.status_code == 200
    body = api.get_json()
    assert body["indices"][0]["pipelines"][0]["signal"] == "LONG"
    assert body["trading"]["running"] is False


def test_live_start_requires_confirmation(tmp_path):
    app = create_app(make_cfg(tmp_path), FakeAPI())
    client = app.test_client()
    resp = client.post("/api/trading/start", json={"mode": "live"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_stop_when_not_running_is_conflict(tmp_path):
    app = create_app(make_cfg(tmp_path), FakeAPI())
    client = app.test_client()
    resp = client.post("/api/trading/stop")
    assert resp.status_code == 409
    resp = client.post("/api/trading/squareoff")
    assert resp.status_code == 409


def test_invalid_mode_rejected(tmp_path):
    app = create_app(make_cfg(tmp_path), FakeAPI())
    client = app.test_client()
    resp = client.post("/api/trading/start", json={"mode": "yolo"})
    assert resp.status_code == 409
    assert "unknown mode" in resp.get_json()["message"]


def test_dashboard_disconnected_when_no_token(tmp_path):
    app = create_app(make_cfg(tmp_path), FakeAPI(token=None))
    client = app.test_client()
    body = client.get("/api/dashboard").get_json()
    assert body["connected"] is False
    assert body["auth"]["connected"] is False
    assert body["indices"] == []


def test_auth_connect_with_manual_token(tmp_path, monkeypatch):
    from nsemomentum import auth

    monkeypatch.setattr(auth, "verify_token", lambda t: {"user_name": "Balaji", "email": "b@x.com"} if t == "good" else None)
    monkeypatch.setattr(auth, "save_token", lambda t: None)

    app = create_app(make_cfg(tmp_path), FakeAPI(token=None))
    client = app.test_client()

    assert client.post("/api/auth/token", json={"access_token": "bad"}).status_code == 400
    resp = client.post("/api/auth/token", json={"access_token": "good"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True

    body = client.get("/api/dashboard").get_json()
    assert body["auth"]["connected"] is True
    assert body["auth"]["profile"]["name"] == "Balaji"


def test_auth_credentials_and_login_url(tmp_path, monkeypatch):
    from nsemomentum import auth

    saved = {}
    monkeypatch.setattr(auth, "save_app_credentials",
                        lambda k, s, r: saved.update(api_key=k, api_secret=s, redirect_uri=r))
    monkeypatch.setattr(auth, "load_app_credentials",
                        lambda: auth.AppCredentials(saved.get("api_key", ""), saved.get("api_secret", ""), saved.get("redirect_uri", "")))

    app = create_app(make_cfg(tmp_path), FakeAPI(token=None))
    client = app.test_client()

    # missing fields rejected
    assert client.post("/api/auth/credentials", json={"api_key": "k"}).status_code == 400
    resp = client.post("/api/auth/credentials",
                       json={"api_key": "k", "api_secret": "s", "redirect_uri": "http://x/callback"})
    assert resp.status_code == 200
    assert "login_url" in resp.get_json() and "client_id=k" in resp.get_json()["login_url"]

    url = client.get("/api/auth/login-url").get_json()
    assert url["ok"] and "client_id=k" in url["login_url"]


def test_settings_apply_and_persist(tmp_path):
    import json

    cfg = make_cfg(tmp_path)
    cfg.paper_trade_log = str(tmp_path / "trades_paper.csv")
    app = create_app(cfg, FakeAPI())
    client = app.test_client()

    resp = client.post("/api/settings", json={"lots_per_trade": 3, "capital": 250000, "daily_profit_target": 5000})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert cfg.lots_per_trade == 3
    assert cfg.paper_starting_cash == 250000.0
    assert cfg.daily_profit_target == 5000.0
    # paper state file cash was reset to the new capital
    with open(cfg.paper_state_file) as fh:
        assert json.load(fh)["cash"] == 250000.0
    # settings echoed in the dashboard payload (core keys; extra flags may be present)
    body = client.get("/api/dashboard").get_json()
    s = body["settings"]
    assert s["lots_per_trade"] == 3 and s["capital"] == 250000.0 and s["daily_profit_target"] == 5000.0
    # overrides persisted for the next start
    with open(tmp_path / "settings.json") as fh:
        saved = json.load(fh)
    assert saved == {"lots_per_trade": 3, "capital": 250000.0, "daily_profit_target": 5000.0}


def test_profit_target_validation(tmp_path):
    app = create_app(make_cfg(tmp_path), FakeAPI())
    client = app.test_client()
    assert client.post("/api/settings", json={"daily_profit_target": -5}).status_code == 400
    assert client.post("/api/settings", json={"daily_profit_target": 0}).status_code == 200


def test_settings_validation(tmp_path):
    app = create_app(make_cfg(tmp_path), FakeAPI())
    client = app.test_client()
    assert client.post("/api/settings", json={"lots_per_trade": 0}).status_code == 400
    assert client.post("/api/settings", json={"lots_per_trade": "x"}).status_code == 400
    assert client.post("/api/settings", json={"capital": 5}).status_code == 400


def test_index_trade_toggle(tmp_path):
    import json

    cfg = make_cfg(tmp_path)
    cfg.instruments = [
        IndexConfig(name="NIFTY", key="NSE_INDEX|Fake", options_available=True),
        IndexConfig(name="SMALLCAP", key="NSE_INDEX|Fake2", options_available=False),
    ]
    app = create_app(cfg, FakeAPI())
    client = app.test_client()

    resp = client.post("/api/index-toggle", json={"name": "NIFTY", "enabled": False})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert cfg.instruments[0].trade_enabled is False
    body = client.get("/api/dashboard").get_json()
    nifty = next(i for i in body["indices"] if i["name"] == "NIFTY")
    assert nifty["trade_enabled"] is False
    # persisted for next start
    with open(tmp_path / "settings.json") as fh:
        assert json.load(fh)["trade_enabled"] == {"NIFTY": False}

    assert client.post("/api/index-toggle", json={"name": "NOPE", "enabled": True}).status_code == 404
    # options-less index can never be trade-enabled
    resp = client.post("/api/index-toggle", json={"name": "SMALLCAP", "enabled": True})
    assert resp.status_code == 400


def test_apply_overrides_restores_toggles(tmp_path):
    import json

    from nsemomentum.settings import apply_overrides

    cfg = make_cfg(tmp_path)
    cfg.instruments = [IndexConfig(name="NIFTY", key="k"), IndexConfig(name="BANKNIFTY", key="k2")]
    with open(tmp_path / "settings.json", "w") as fh:
        json.dump({"lots_per_trade": 2, "trade_enabled": {"BANKNIFTY": False}}, fh)
    apply_overrides(cfg)
    assert cfg.lots_per_trade == 2
    assert cfg.instruments[0].trade_enabled is True
    assert cfg.instruments[1].trade_enabled is False


def test_trade_log_endpoints(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.paper_trade_log = str(tmp_path / "trades_paper.csv")
    cfg.live_trade_log = str(tmp_path / "trades_live.csv")

    from nsemomentum.broker import PaperBroker

    broker = PaperBroker(cfg, None)
    broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY 25500 CE", 75, "LONG", 100.0)
    broker.exit("NIFTY:1m", price_hint=110.0)

    app = create_app(cfg, FakeAPI())
    client = app.test_client()

    body = client.get("/api/trades?mode=paper").get_json()
    assert body["mode"] == "paper"
    assert len(body["trades"]) == 2
    assert body["trades"][0]["action"].startswith("EXIT")  # newest first
    assert float(body["trades"][0]["pnl"]) > 0

    dl = client.get("/trades.csv?mode=paper")
    assert dl.status_code == 200
    assert "attachment" in dl.headers.get("Content-Disposition", "")
    assert b"NIFTY 25500 CE" in dl.data

    assert client.get("/api/trades?mode=live").get_json()["trades"] == []
    assert client.get("/trades.csv?mode=live").status_code == 404
