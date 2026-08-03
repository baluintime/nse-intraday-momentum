"""Web dashboard: fetches historical + intraday candles from Upstox, computes
the Ichimoku Cloud server-side for every index on both timeframes, and serves
an auto-refreshing animated page (no charts — signal cards and level ladders).

Run with:  python -m nsemomentum web
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import os
import threading
import time as _time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file

from . import auth
from . import settings as settings_mod

from .candles import Candle, TimeframeAggregator
from .config import Config, IndexConfig, MomentumSymbol
from .strategy import StrategyConfig, evaluate
from .tzutil import get_zone
from .upstox_api import UpstoxAPI, UpstoxError

log = logging.getLogger(__name__)

LEVEL_NAMES = [
    ("tenkan", "Tenkan-sen"),
    ("kijun", "Kijun-sen"),
    ("span_a", "Senkou Span A"),
    ("span_b", "Senkou Span B"),
]


def analyze_series(candles: list[Candle], sc: StrategyConfig) -> dict | None:
    """Snapshot of the latest completed candle using the full strategy rules
    (Ichimoku + optional MACD / Chikou / cloud-thickness filters)."""
    if not candles:
        return None
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    last = candles[-1]
    ev = evaluate(highs, lows, closes, sc)
    if ev is None:
        return {
            "ready": False,
            "candles": len(candles),
            "needed": sc.min_candles,
            "close": last.close,
            "candle_time": last.ts.strftime("%H:%M"),
            "candle_date": last.ts.strftime("%Y-%m-%d"),
        }
    state = ev.state
    close = ev.close
    if close > state.cloud_top:
        zone = "ABOVE CLOUD"
    elif close < state.cloud_bottom:
        zone = "BELOW CLOUD"
    else:
        zone = "IN CLOUD"
    levels = []
    for attr, label in LEVEL_NAMES:
        value = getattr(state, attr)
        levels.append(
            {
                "name": label,
                "value": round(value, 2),
                "above": close > value,
                "below": close < value,
                "dist_pct": round((close - value) / value * 100, 3) if value else 0.0,
            }
        )
    return {
        "ready": True,
        "candles": len(candles),
        "close": close,
        "candle_time": last.ts.strftime("%H:%M"),
        "candle_date": last.ts.strftime("%Y-%m-%d"),
        "signal": ev.signal,
        "zone": zone,
        "levels": levels,
        "macd_hist": round(ev.macd_hist, 2) if ev.macd_hist is not None else None,
        "thickness": round(ev.thickness, 2) if ev.thickness is not None else None,
        "filters": {
            "macd": None if not sc.use_macd else (ev.macd_hist is not None and (
                ev.macd_hist > 0 if ev.signal != "SHORT" else ev.macd_hist < 0)),
            "chikou": None if not sc.use_chikou else (
                ev.chikou_ok_long if ev.signal != "SHORT" else ev.chikou_ok_short),
            "thickness_ok": ev.thickness_ok,
        },
        "cloud": {
            "top": round(state.cloud_top, 2),
            "bottom": round(state.cloud_bottom, 2),
            "bullish": state.span_a >= state.span_b,
            "thickness_pct": round(
                (state.cloud_top - state.cloud_bottom) / close * 100, 3
            ) if close else 0.0,
        },
    }


class DashboardService:
    """Builds the dashboard payload, caching Upstox data briefly so several
    open browser tabs don't multiply API calls."""

    def __init__(self, cfg: Config, api: UpstoxAPI):
        self.cfg = cfg
        self.api = api
        self.tz = get_zone(cfg.timezone)
        from .strategy import build_strategy_config
        self.sc = build_strategy_config(cfg)
        self.params = self.sc.ich
        # per-index StrategyConfig so the min_cloud_thickness gate matches each
        # index's point scale (the dashboard mirrors the engine's per-index sc)
        self._sc_by_key = {
            ix.key: build_strategy_config(cfg, ix) for ix in cfg.instruments
        }
        self.ttl = max(3.0, cfg.web_refresh_seconds / 2.0)
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0
        # historical candles are immutable — fetch once per (index, day)
        self._hist: dict[str, tuple[str, list[Candle]]] = {}
        # failing indices are paused for a while instead of retried every poll
        self.fail_cooldown = 300.0
        self._failed: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------- data

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _historical(self, index: IndexConfig, today: str) -> list[Candle]:
        cached = self._hist.get(index.key)
        if cached and cached[0] == today:
            return cached[1]
        now = self._now()
        to_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=self.cfg.warmup_days)).strftime("%Y-%m-%d")
        rows = self.api.historical_candles(index.key, to_date, from_date)
        candles = sorted((Candle.from_upstox(r) for r in rows), key=lambda c: c.ts)
        self._hist[index.key] = (today, candles)
        return candles

    def _index_candles(self, index: IndexConfig) -> list[Candle]:
        """Completed 1m candles: cached historical warmup + fresh intraday."""
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        candles = list(self._historical(index, today))
        rows = self.api.intraday_candles(index.key)
        cutoff = now.replace(second=0, microsecond=0)
        intraday = sorted(
            (
                c
                for r in rows
                if (c := Candle.from_upstox(r)).ts + timedelta(minutes=1) <= cutoff
            ),
            key=lambda c: c.ts,
        )
        last_ts = candles[-1].ts if candles else None
        candles.extend(c for c in intraday if last_ts is None or c.ts > last_ts)
        return candles

    # ---------------------------------------------------------- payload

    def payload(self) -> dict:
        with self._lock:
            if not self.api.has_token:
                # no cache while disconnected — cheap and always current
                return {
                    "connected": False,
                    "generated_at": self._now().strftime("%H:%M:%S"),
                    "generated_date": self._now().strftime("%a, %d %b %Y"),
                    "refresh_seconds": self.cfg.web_refresh_seconds,
                    "market": {"status": self._market_status(self._now()),
                               "session": f"{self.cfg.market_open.strftime('%H:%M')}–{self.cfg.market_close.strftime('%H:%M')} IST"},
                    "strategy": {
                        "params": f"{self.params.tenkan}/{self.params.kijun}/{self.params.senkou_b} (disp {self.params.displacement})",
                        "timeframes": [f"{tf}m" for tf in self.cfg.timeframes_minutes],
                    },
                    "error": None,
                    "indices": [],
                    "paper": None,
                }
            if self._cached is not None and _time.monotonic() - self._cached_at < self.ttl:
                return self._cached
            data = self._build()
            self._cached, self._cached_at = data, _time.monotonic()
            return data

    def _market_status(self, now: datetime) -> str:
        if now.weekday() >= 5:
            return "CLOSED"
        t = now.time()
        if self.cfg.market_open <= t <= self.cfg.market_close:
            return "OPEN"
        return "PRE-OPEN" if t < self.cfg.market_open else "CLOSED"

    def _build(self) -> dict:
        now = self._now()
        indices_payload: list[dict] = []
        error: str | None = None

        keys = [ix.key for ix in self.cfg.enabled_instruments]
        ltps: dict[str, float] = {}
        try:
            ltps = self.api.ltp(keys) if keys else {}
        except UpstoxError as exc:
            log.warning("ltp fetch failed: %s", exc)

        for index in self.cfg.enabled_instruments:
            entry: dict = {
                "name": index.name,
                "key": index.key,
                "options_available": index.options_available,
                "trade_enabled": index.trade_enabled,
                "ltp": ltps.get(index.key),
                "prev_close": None,
                "change_pct": None,
                "pipelines": [],
                "error": None,
            }
            paused = self._failed.get(index.key)
            if paused and _time.monotonic() - paused[0] < self.fail_cooldown:
                entry["error"] = paused[1] + " (retry paused)"
                indices_payload.append(entry)
                error = error or "Some indices failed to load — see index cards."
                continue
            try:
                one_min = self._index_candles(index)
                self._failed.pop(index.key, None)
            except UpstoxError as exc:
                msg = str(exc)[:200]
                self._failed[index.key] = (_time.monotonic(), msg)
                entry["error"] = msg
                log.warning(
                    "candle fetch failed for %s (pausing retries for %.0fs): %s",
                    index.name, self.fail_cooldown, exc,
                )
                indices_payload.append(entry)
                error = error or "Some indices failed to load — see index cards."
                continue

            today = now.date()
            prev = [c for c in one_min if c.ts.date() < today]
            if prev:
                entry["prev_close"] = prev[-1].close
                ref = entry["ltp"] or (one_min[-1].close if one_min else None)
                if ref:
                    entry["change_pct"] = round((ref - prev[-1].close) / prev[-1].close * 100, 2)
            if entry["ltp"] is None and one_min:
                entry["ltp"] = one_min[-1].close

            for tf in self.cfg.timeframes_minutes:
                if tf == 1:
                    series = one_min
                else:
                    agg = TimeframeAggregator(tf)
                    series = [done for c in one_min if (done := agg.feed(c))]
                sc = self._sc_by_key.get(index.key, self.sc)
                analysis = analyze_series(series, sc) or {"ready": False, "candles": 0}
                analysis["timeframe"] = f"{tf}m"
                entry["pipelines"].append(analysis)
            indices_payload.append(entry)

        return {
            "connected": True,
            "generated_at": now.strftime("%H:%M:%S"),
            "generated_date": now.strftime("%a, %d %b %Y"),
            "refresh_seconds": self.cfg.web_refresh_seconds,
            "market": {
                "status": self._market_status(now),
                "session": f"{self.cfg.market_open.strftime('%H:%M')}–{self.cfg.market_close.strftime('%H:%M')} IST",
            },
            "strategy": {
                "params": f"{self.params.tenkan}/{self.params.kijun}/{self.params.senkou_b} (disp {self.params.displacement})",
                "timeframes": [f"{tf}m" for tf in self.cfg.timeframes_minutes],
            },
            "error": error,
            "indices": indices_payload,
            "paper": self._paper_state(),
        }

    def _paper_state(self) -> dict | None:
        try:
            with open(self.cfg.paper_state_file, encoding="utf-8") as fh:
                state = json.load(fh)
        except (FileNotFoundError, ValueError):
            return None
        positions = [
            {
                "pipeline": pid,
                "symbol": pos.get("symbol"),
                "strike": pos.get("strike"),
                "direction": pos.get("direction"),
                "qty": pos.get("qty"),
                "entry_price": pos.get("entry_price"),
            }
            for pid, pos in (state.get("positions") or {}).items()
        ]
        return {
            "cash": state.get("cash"),
            "realized_pnl_today": state.get("realized_pnl_today"),
            "pnl_date": state.get("pnl_date"),
            "positions": positions,
        }


def _callback_page(ok: bool, message: str) -> str:
    color = "#22c55e" if ok else "#f4405f"
    icon = "✓" if ok else "✕"
    safe = message.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>NSE Momentum — Upstox</title><style>
  body {{ background:#060913; color:#e8edf7; font:15px/1.5 system-ui,sans-serif;
         display:grid; place-items:center; height:100vh; margin:0; }}
  .box {{ text-align:center; padding:36px 44px; border-radius:18px;
          background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.1); }}
  .icon {{ font-size:44px; color:{color}; }}
  h2 {{ margin:12px 0 6px; }} p {{ color:#8b96ad; max-width:380px; }}
  a {{ color:#38bdf8; }}
</style></head><body><div class="box">
  <div class="icon">{icon}</div>
  <h2>{"Connected" if ok else "Connection failed"}</h2>
  <p>{safe}</p>
  <p><a href="/">← Back to dashboard</a></p>
  <script>{"setTimeout(function(){window.location='/';}, 1500);" if ok else ""}</script>
</div></body></html>"""


def read_trade_log(path: str, limit: int = 200) -> list[dict]:
    """Last `limit` trades from a broker CSV log, newest first."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error) as exc:
        log.warning("could not read trade log %s: %s", path, exc)
        return []
    return rows[-limit:][::-1]


def _momentum_config_dict(cfg: Config) -> dict:
    return {
        "min_gap_pct": cfg.mom_min_gap_pct,
        "min_rvol": cfg.mom_min_rvol,
        "min_oi_change_pct": cfg.mom_min_oi_change_pct,
        "min_depth_ratio": cfg.mom_min_depth_ratio,
        "opening_window_minutes": cfg.mom_opening_window_minutes,
        "require_confirmation": cfg.mom_require_confirmation,
        "require_pdh_pdl": cfg.mom_require_pdh_pdl,
        "require_cvd": cfg.mom_require_cvd,
        "require_chikou": cfg.mom_require_chikou,
        "require_tenkan_kijun": cfg.mom_require_tenkan_kijun,
        "delta_min": cfg.mom_delta_min,
        "delta_max": cfg.mom_delta_max,
        "timeframes": [f"{tf}m" for tf in cfg.mom_timeframes_minutes],
        "symbols": [
            {"name": s.name, "key": s.key, "futures_key": s.futures_key, "enabled": s.enabled}
            for s in cfg.momentum_symbols
        ],
    }


_MOM_FLOAT_FIELDS = {
    "min_gap_pct": "mom_min_gap_pct",
    "min_rvol": "mom_min_rvol",
    "min_oi_change_pct": "mom_min_oi_change_pct",
    "min_depth_ratio": "mom_min_depth_ratio",
    "delta_min": "mom_delta_min",
    "delta_max": "mom_delta_max",
}
_MOM_BOOL_FIELDS = {
    "require_confirmation": "mom_require_confirmation",
    "require_pdh_pdl": "mom_require_pdh_pdl",
    "require_cvd": "mom_require_cvd",
    "require_chikou": "mom_require_chikou",
    "require_tenkan_kijun": "mom_require_tenkan_kijun",
}


def _apply_momentum_config(cfg: Config, momentum, body: dict) -> tuple[bool, str]:
    changed = []
    for field, attr in _MOM_FLOAT_FIELDS.items():
        if field in body and body[field] is not None:
            try:
                val = float(body[field])
            except (TypeError, ValueError):
                return False, f"{field} must be a number"
            if val < 0:
                return False, f"{field} cannot be negative"
            setattr(cfg, attr, val)
            changed.append(field)
    if "opening_window_minutes" in body and body["opening_window_minutes"] is not None:
        try:
            val = int(body["opening_window_minutes"])
        except (TypeError, ValueError):
            return False, "opening_window_minutes must be an integer"
        if val < 1:
            return False, "opening_window_minutes must be >= 1"
        cfg.mom_opening_window_minutes = val
        changed.append("opening_window_minutes")
    for field, attr in _MOM_BOOL_FIELDS.items():
        if field in body:
            setattr(cfg, attr, bool(body[field]))
            changed.append(field)
    if cfg.mom_delta_min > cfg.mom_delta_max:
        return False, "delta_min cannot exceed delta_max"
    # rebuild the service's cached MomentumConfig and drop stale results
    momentum.mcfg = cfg.momentum_config()
    momentum.invalidate()
    return True, f"updated: {', '.join(changed)}" if changed else "nothing to change"


def _mutate_momentum_symbols(cfg: Config, momentum, body: dict) -> tuple[bool, str]:
    action = str(body.get("action", "")).lower()
    name = str(body.get("name", "")).strip()
    if action == "add":
        key = str(body.get("key", "")).strip()
        if not (name and key):
            return False, "name and Upstox instrument key are required"
        if any(s.name.lower() == name.lower() for s in cfg.momentum_symbols):
            return False, f"{name} is already on the watchlist"
        futures = str(body.get("futures_key", "")).strip() or None
        cfg.momentum_symbols.append(MomentumSymbol(name=name, key=key, futures_key=futures))
        momentum.invalidate()
        return True, f"added {name}"
    sym = next((s for s in cfg.momentum_symbols if s.name.lower() == name.lower()), None)
    if sym is None:
        return False, f"unknown symbol {name!r}"
    if action == "remove":
        cfg.momentum_symbols.remove(sym)
        momentum.invalidate()
        return True, f"removed {name}"
    if action == "toggle":
        sym.enabled = bool(body.get("enabled", not sym.enabled))
        momentum.invalidate()
        return True, f"{name}: {'ON' if sym.enabled else 'OFF'}"
    return False, f"unknown action {action!r}"


class TradingController:
    """Starts/stops the trading Engine in a background thread, switchable
    between paper and live mode from the dashboard."""

    def __init__(self, cfg: Config, api: UpstoxAPI, token: str | None = None, momentum=None):
        self.base_cfg = cfg
        self.api = api
        self.token = token
        self.momentum = momentum  # MomentumService, so the engine can trade its picks
        self._lock = threading.Lock()
        self._engine = None
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self.last_error: str | None = None
        self.started_at: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, mode: str) -> tuple[bool, str]:
        from .engine import Engine

        with self._lock:
            if self.running:
                return False, "engine is already running — stop it first"
            if mode not in ("paper", "live"):
                return False, f"unknown mode {mode!r}"
            cfg = copy.copy(self.base_cfg)
            cfg.mode = mode
            api = UpstoxAPI(self.token) if self.token else self.api
            try:
                engine = Engine(cfg, api, momentum=self.momentum)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                return False, self.last_error
            stop = threading.Event()

            def _run() -> None:
                try:
                    engine.run(stop)
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("engine crashed")

            self.last_error = None
            self._engine = engine
            self._stop = stop
            self._thread = threading.Thread(target=_run, daemon=True, name=f"engine-{mode}")
            self.started_at = datetime.now().strftime("%H:%M:%S")
            self._thread.start()
            log.info("engine started from dashboard in %s mode", mode.upper())
            return True, f"{mode.upper()} engine started"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if not self.running:
                return False, "engine is not running"
            assert self._stop is not None and self._thread is not None
            self._stop.set()
            self._thread.join(timeout=15)
            log.info("engine stopped from dashboard")
            return True, "engine stopped (open positions left untouched)"

    def square_off(self) -> tuple[bool, str]:
        engine = self._engine
        if engine is None or not self.running:
            return False, "engine is not running"
        engine.square_off_all("manual (dashboard)")
        return True, "square-off requested for all open positions"

    def status(self) -> dict:
        running = self.running
        st: dict = {
            "running": running,
            "mode": self._engine.cfg.mode if (running and self._engine) else None,
            "started_at": self.started_at if running else None,
            "last_error": self.last_error,
            "positions": [],
            "realized_pnl_today": None,
            "unrealized_pnl": None,
            "total_pnl": None,
            "profit_target": None,
            "halted": False,
            "halt_reason": None,
            "sync_ok": True,
            "position_mismatch": [],
            "cash": None,
            "skips": [],
        }
        if running and self._engine is not None:
            engine = self._engine
            broker = engine.broker
            st["sync_ok"] = engine._sync_ok
            st["position_mismatch"] = engine._position_mismatch
            realized = broker.realized_pnl_today()
            unrealized, detail = broker.mark_to_market()
            st["realized_pnl_today"] = realized
            st["unrealized_pnl"] = unrealized
            st["total_pnl"] = realized + unrealized
            st["profit_target"] = engine.cfg.daily_profit_target or None
            st["halted"] = engine._halted
            st["halt_reason"] = engine._halt_reason or None
            if engine.cfg.mode == "paper":
                st["cash"] = broker.state.cash
            st["positions"] = [
                {
                    "pipeline": p.pipeline_id,
                    "symbol": p.symbol,
                    "strike": p.strike,
                    "direction": p.direction,
                    "qty": p.qty,
                    "entry_price": p.entry_price,
                    "ltp": (detail.get(p.pipeline_id) or {}).get("ltp"),
                    "upnl": (detail.get(p.pipeline_id) or {}).get("upnl"),
                }
                for p in broker.open_positions()
            ]
            st["skips"] = self._engine.recent_skips()
        return st


class AuthManager:
    """Handles connecting to Upstox from the web page: app credentials, the
    OAuth login URL, code exchange, and token verification. Keeps the shared
    UpstoxAPI and TradingController tokens in sync."""

    def __init__(self, api: UpstoxAPI, controller: "TradingController"):
        self.api = api
        self.controller = controller
        self.profile: dict | None = None

    def _apply_token(self, token: str) -> None:
        self.api.set_token(token)
        self.controller.token = token
        auth.save_token(token)

    def verify_stored(self) -> None:
        """Called once at startup: drop a stale overnight token so the page
        shows 'disconnected' instead of failing every data fetch."""
        if not self.api.has_token:
            return
        self.profile = auth.verify_token(self.api.access_token or "")
        if self.profile is None:
            log.info("stored Upstox token is expired/invalid — starting disconnected")
            self.api.set_token(None)
            self.controller.token = None

    def status(self) -> dict:
        creds = auth.load_app_credentials()
        connected = self.api.has_token
        return {
            "connected": connected,
            "credentials_set": creds.complete,
            "redirect_uri": creds.redirect_uri,
            "api_key_hint": (creds.api_key[:4] + "…") if creds.api_key else "",
            "profile": {
                "name": (self.profile or {}).get("user_name") or (self.profile or {}).get("name"),
                "email": (self.profile or {}).get("email"),
                "user_id": (self.profile or {}).get("user_id"),
            } if self.profile else None,
        }

    def save_credentials(self, api_key: str, api_secret: str, redirect_uri: str) -> tuple[bool, str]:
        if not (api_key.strip() and api_secret.strip() and redirect_uri.strip()):
            return False, "api key, secret and redirect URI are all required"
        auth.save_app_credentials(api_key, api_secret, redirect_uri)
        return True, "credentials saved — click Connect Upstox to log in"

    def login_url(self) -> tuple[bool, str]:
        creds = auth.load_app_credentials()
        if not creds.complete:
            return False, "enter your Upstox app credentials first"
        return True, auth.build_login_url(creds.api_key, creds.redirect_uri, state="nsemomentum")

    def complete_with_code(self, code: str) -> tuple[bool, str]:
        creds = auth.load_app_credentials()
        if not creds.complete:
            return False, "app credentials are not configured"
        if not code.strip():
            return False, "authorization code is empty"
        try:
            token = auth.exchange_code(code, creds.api_key, creds.api_secret, creds.redirect_uri)
        except auth.AuthError as exc:
            return False, str(exc)
        self.profile = auth.verify_token(token)
        self._verified = True
        self._apply_token(token)
        name = (self.profile or {}).get("user_name") or "your account"
        return True, f"connected to Upstox as {name}"

    def set_manual_token(self, token: str) -> tuple[bool, str]:
        token = token.strip()
        if not token:
            return False, "access token is empty"
        profile = auth.verify_token(token)
        if profile is None:
            return False, "that access token was rejected by Upstox (expired or invalid)"
        self.profile = profile
        self._verified = True
        self._apply_token(token)
        return True, f"connected as {profile.get('user_name') or 'your account'}"

    def logout(self) -> tuple[bool, str]:
        if self.controller.running:
            return False, "stop the trading engine before disconnecting"
        self.api.set_token(None)
        self.controller.token = None
        self.profile = None
        self._verified = True
        try:
            if auth.TOKEN_FILE.exists():
                auth.TOKEN_FILE.unlink()
        except OSError:
            pass
        return True, "disconnected from Upstox"


def create_app(cfg: Config, api: UpstoxAPI, token: str | None = None) -> Flask:
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
    from .momentum_web import MomentumService
    service = DashboardService(cfg, api)
    momentum = MomentumService(cfg, api)
    controller = TradingController(cfg, api, token, momentum=momentum)
    auth_mgr = AuthManager(api, controller)

    @app.get("/")
    @app.get("/momentum")
    def momentum_page():  # type: ignore[unused-variable]
        # NSE Momentum is the primary product — it is the home page.
        return render_template("momentum.html", refresh_seconds=cfg.web_refresh_seconds)

    @app.get("/ichimoku")
    def dashboard():  # type: ignore[unused-variable]
        # The Ichimoku Cloud dashboard is a secondary, linked tool.
        return render_template("dashboard.html", refresh_seconds=cfg.web_refresh_seconds)

    @app.get("/api/momentum")
    def api_momentum():  # type: ignore[unused-variable]
        payload = dict(momentum.payload())
        payload["auth"] = auth_mgr.status()
        return jsonify(payload)

    @app.get("/api/momentum/config")
    def api_momentum_config_get():  # type: ignore[unused-variable]
        return jsonify(_momentum_config_dict(cfg))

    @app.post("/api/momentum/config")
    def api_momentum_config_set():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        ok, msg = _apply_momentum_config(cfg, momentum, body)
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

    @app.post("/api/momentum/symbols")
    def api_momentum_symbols():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        ok, msg = _mutate_momentum_symbols(cfg, momentum, body)
        return jsonify({"ok": ok, "message": msg, "symbols": [
            {"name": s.name, "key": s.key, "futures_key": s.futures_key, "enabled": s.enabled}
            for s in cfg.momentum_symbols
        ]}), (200 if ok else 400)

    @app.post("/api/momentum/scan")
    def api_momentum_scan():  # type: ignore[unused-variable]
        if not momentum.api.has_token:
            return jsonify({"ok": False, "message": "connect Upstox first"}), 400
        started = momentum.scan_async()
        return jsonify({
            "ok": started,
            "message": "scanning the NSE F&O universe…" if started else "a scan is already running",
        }), (200 if started else 409)

    @app.get("/api/dashboard")
    def api_dashboard():  # type: ignore[unused-variable]
        payload = dict(service.payload())
        payload["trading"] = controller.status()
        payload["auth"] = auth_mgr.status()
        payload["settings"] = {
            "lots_per_trade": cfg.lots_per_trade,
            "capital": cfg.paper_starting_cash,
            "daily_profit_target": cfg.daily_profit_target,
        }
        return jsonify(payload)

    # ------------------------------------------------------------- auth

    @app.get("/api/auth/status")
    def auth_status():  # type: ignore[unused-variable]
        return jsonify(auth_mgr.status())

    @app.post("/api/auth/credentials")
    def auth_credentials():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        ok, msg = auth_mgr.save_credentials(
            str(body.get("api_key", "")),
            str(body.get("api_secret", "")),
            str(body.get("redirect_uri", "")),
        )
        result = {"ok": ok, "message": msg}
        if ok:
            lok, url = auth_mgr.login_url()
            result["login_url"] = url if lok else None
        return jsonify(result), (200 if ok else 400)

    @app.get("/api/auth/login-url")
    def auth_login_url():  # type: ignore[unused-variable]
        ok, url = auth_mgr.login_url()
        return jsonify({"ok": ok, "login_url": url if ok else None, "message": None if ok else url}), (200 if ok else 400)

    @app.post("/api/auth/code")
    def auth_code():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        ok, msg = auth_mgr.complete_with_code(str(body.get("code", "")))
        if ok:
            service._cached = None
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

    @app.post("/api/auth/token")
    def auth_token():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        ok, msg = auth_mgr.set_manual_token(str(body.get("access_token", "")))
        if ok:
            service._cached = None
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

    @app.post("/api/auth/logout")
    def auth_logout():  # type: ignore[unused-variable]
        ok, msg = auth_mgr.logout()
        if ok:
            service._cached = None
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

    @app.get("/callback")
    def auth_callback():  # type: ignore[unused-variable]
        """Upstox redirects here after login (when the app's redirect URI is
        set to http://<host>:<port>/callback). Exchanges the code and shows a
        small confirmation page that returns to the dashboard."""
        code = request.args.get("code", "")
        err = request.args.get("error_description") or request.args.get("error")
        if err:
            return _callback_page(False, f"Upstox returned an error: {err}"), 400
        ok, msg = auth_mgr.complete_with_code(code)
        if ok:
            service._cached = None
        return _callback_page(ok, msg), (200 if ok else 400)

    @app.post("/api/settings")
    def api_settings():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        lots = body.get("lots_per_trade")
        capital = body.get("capital")
        profit_target = body.get("daily_profit_target")
        err = settings_mod.validate(lots, capital, profit_target)
        if err:
            return jsonify({"ok": False, "message": err}), 400
        messages = []
        if lots is not None:
            cfg.lots_per_trade = int(lots)
            engine = controller._engine
            if controller.running and engine is not None:
                engine.cfg.lots_per_trade = int(lots)
            messages.append(f"lots per trade set to {int(lots)} (applies to new entries)")
        if profit_target is not None:
            pt = float(profit_target)
            cfg.daily_profit_target = pt
            engine = controller._engine
            if controller.running and engine is not None:
                engine.cfg.daily_profit_target = pt
            messages.append(
                f"daily profit target set to ₹{pt:,.0f}" if pt > 0 else "daily profit target disabled"
            )
        if capital is not None:
            if controller.running and controller.status().get("mode") == "paper":
                return jsonify(
                    {"ok": False, "message": "stop the paper engine before changing capital"}
                ), 409
            capital = float(capital)
            cfg.paper_starting_cash = capital
            # reset the paper account's free cash to the new capital
            state = {}
            if os.path.exists(cfg.paper_state_file):
                try:
                    with open(cfg.paper_state_file, encoding="utf-8") as fh:
                        state = json.load(fh)
                except (ValueError, OSError):
                    state = {}
            state["cash"] = capital
            os.makedirs(os.path.dirname(cfg.paper_state_file) or ".", exist_ok=True)
            with open(cfg.paper_state_file, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
            messages.append(f"paper capital set to ₹{capital:,.0f}")
        settings_mod.save_overrides(cfg, lots=lots, capital=capital, profit_target=profit_target)
        service._cached = None  # bust cache so the strip updates immediately
        return jsonify({"ok": True, "message": "; ".join(messages) or "nothing to change"})

    @app.post("/api/index-toggle")
    def api_index_toggle():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", ""))
        enabled = bool(body.get("enabled", True))
        # cfg.instruments objects are shared with any running engine (the
        # engine start uses a shallow config copy), so this applies instantly
        index = next((ix for ix in cfg.instruments if ix.name == name), None)
        if index is None:
            return jsonify({"ok": False, "message": f"unknown index {name!r}"}), 404
        if not index.options_available and enabled:
            return jsonify(
                {"ok": False, "message": f"{name} has no listed options — it is always signal-only"}
            ), 400
        index.trade_enabled = enabled
        settings_mod.save_overrides(cfg, trade_toggle=(name, enabled))
        service._cached = None
        state = "ON" if enabled else "OFF (signals still shown; open positions still managed)"
        return jsonify({"ok": True, "message": f"trading for {name}: {state}"})

    @app.get("/api/trades")
    def api_trades():  # type: ignore[unused-variable]
        mode = request.args.get("mode", "paper")
        path = cfg.live_trade_log if mode == "live" else cfg.paper_trade_log
        return jsonify({"mode": mode, "trades": read_trade_log(path)})

    @app.get("/trades.csv")
    def trades_csv():  # type: ignore[unused-variable]
        mode = request.args.get("mode", "paper")
        path = cfg.live_trade_log if mode == "live" else cfg.paper_trade_log
        if not os.path.exists(path):
            return jsonify({"ok": False, "message": f"no {mode} trades logged yet"}), 404
        return send_file(
            os.path.abspath(path),
            as_attachment=True,
            download_name=f"nsemomentum_trades_{mode}_{datetime.now().strftime('%Y%m%d')}.csv",
            mimetype="text/csv",
        )

    @app.post("/api/trading/start")
    def trading_start():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        mode = str(body.get("mode", "paper")).lower()
        if mode == "live" and body.get("confirm") != "LIVE":
            return jsonify({"ok": False, "message": 'LIVE mode places real orders — confirmation "LIVE" required'}), 400
        ok, msg = controller.start(mode)
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

    @app.post("/api/trading/stop")
    def trading_stop():  # type: ignore[unused-variable]
        ok, msg = controller.stop()
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

    @app.post("/api/trading/squareoff")
    def trading_squareoff():  # type: ignore[unused-variable]
        ok, msg = controller.square_off()
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

    app.auth_manager = auth_mgr  # type: ignore[attr-defined]
    app.momentum_service = momentum  # type: ignore[attr-defined]
    return app


def run_web(cfg: Config, api: UpstoxAPI, token: str | None = None) -> None:
    app = create_app(cfg, api, token)
    app.auth_manager.verify_stored()  # type: ignore[attr-defined]
    momentum = app.momentum_service  # type: ignore[attr-defined]
    if cfg.mom_auto_universe:
        stop = threading.Event()
        threading.Thread(
            target=momentum.run_scheduler, args=(stop,), daemon=True, name="momentum-scheduler"
        ).start()
        log.info(
            "momentum auto-scan enabled — scanning the NSE F&O universe at %s IST",
            ", ".join(t.strftime("%H:%M") for t in cfg.mom_scan_times),
        )
    log.info("dashboard on http://%s:%d (refresh every %ds)", cfg.web_host, cfg.web_port, cfg.web_refresh_seconds)
    app.run(host=cfg.web_host, port=cfg.web_port, debug=False, threaded=True)
