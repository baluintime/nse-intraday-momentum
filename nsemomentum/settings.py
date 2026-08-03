"""User-adjustable runtime settings (lots per trade, paper capital).

Values changed from the dashboard are persisted to a small JSON file next to
the paper state so they survive restarts, without rewriting config.yaml.
"""

from __future__ import annotations

import json
import logging
import os

from .config import Config

log = logging.getLogger(__name__)

MIN_LOTS, MAX_LOTS = 1, 100
MIN_CAPITAL, MAX_CAPITAL = 1_000.0, 1_000_000_000.0


def settings_path(cfg: Config) -> str:
    return os.path.join(os.path.dirname(cfg.paper_state_file) or ".", "settings.json")


def apply_overrides(cfg: Config) -> None:
    """Apply persisted UI overrides on top of config.yaml values."""
    path = settings_path(cfg)
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return
    if "lots_per_trade" in data:
        cfg.lots_per_trade = int(data["lots_per_trade"])
    if "capital" in data:
        cfg.paper_starting_cash = float(data["capital"])
    if "daily_profit_target" in data:
        cfg.daily_profit_target = float(data["daily_profit_target"])
    toggles = data.get("trade_enabled") or {}
    for ix in cfg.instruments:
        if ix.name in toggles:
            ix.trade_enabled = bool(toggles[ix.name])


def save_overrides(
    cfg: Config,
    lots: int | None = None,
    capital: float | None = None,
    trade_toggle: tuple[str, bool] | None = None,
    profit_target: float | None = None,
) -> None:
    path = settings_path(cfg)
    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (ValueError, OSError):
            data = {}
    if lots is not None:
        data["lots_per_trade"] = int(lots)
    if capital is not None:
        data["capital"] = float(capital)
    if profit_target is not None:
        data["daily_profit_target"] = float(profit_target)
    if trade_toggle is not None:
        name, enabled = trade_toggle
        data.setdefault("trade_enabled", {})[name] = bool(enabled)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def validate(lots, capital, profit_target=None) -> str | None:
    """Returns an error message, or None if values are acceptable."""
    if lots is not None:
        try:
            lots = int(lots)
        except (TypeError, ValueError):
            return "lots_per_trade must be a whole number"
        if not MIN_LOTS <= lots <= MAX_LOTS:
            return f"lots_per_trade must be between {MIN_LOTS} and {MAX_LOTS}"
    if capital is not None:
        try:
            capital = float(capital)
        except (TypeError, ValueError):
            return "capital must be a number"
        if not MIN_CAPITAL <= capital <= MAX_CAPITAL:
            return f"capital must be between {MIN_CAPITAL:,.0f} and {MAX_CAPITAL:,.0f}"
    if profit_target is not None:
        try:
            pt = float(profit_target)
        except (TypeError, ValueError):
            return "profit target must be a number"
        if pt < 0:
            return "profit target must be 0 (disabled) or a positive number"
    return None
