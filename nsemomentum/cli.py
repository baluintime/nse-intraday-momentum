"""Command-line interface for NSE Momentum."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import auth
from .config import load_config
from .upstox_api import UpstoxAPI


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_login(_args: argparse.Namespace) -> None:
    auth.interactive_login()


def cmd_instruments(args: argparse.Namespace) -> None:
    rows = UpstoxAPI.download_nse_instruments()
    needle = (args.search or "").lower()
    hits = 0
    for row in rows:
        if args.index_only and row.get("segment") != "NSE_INDEX":
            continue
        text = " ".join(
            str(row.get(k, "")) for k in ("instrument_key", "trading_symbol", "name")
        ).lower()
        if needle and needle not in text:
            continue
        print(f"{row.get('instrument_key', ''):<40} {row.get('trading_symbol', ''):<28} {row.get('name', '')}")
        hits += 1
        if hits >= args.limit:
            print(f"... (limited to {args.limit}; refine with --search)")
            break
    if hits == 0:
        print("No instruments matched.")


def cmd_run(args: argparse.Namespace) -> None:
    from .engine import Engine

    cfg = load_config(args.config)
    if args.mode:
        cfg.mode = args.mode
    if cfg.mode not in ("paper", "live"):
        raise SystemExit(f"Unknown mode {cfg.mode!r}; use paper or live.")
    if cfg.mode == "live" and not args.yes:
        print("LIVE mode places REAL orders with REAL money on your Upstox account.")
        if input("Type 'live' to confirm: ").strip().lower() != "live":
            raise SystemExit("Aborted.")
    from .keyresolver import resolve_index_keys
    from .settings import apply_overrides

    apply_overrides(cfg)
    resolve_index_keys(cfg.instruments)
    api = UpstoxAPI(auth.load_token())
    Engine(cfg, api).run()


def cmd_web(args: argparse.Namespace) -> None:
    from .keyresolver import resolve_index_keys
    from .web import run_web

    cfg = load_config(args.config)
    if args.host:
        cfg.web_host = args.host
    if args.port:
        cfg.web_port = args.port
    from .settings import apply_overrides

    apply_overrides(cfg)
    resolve_index_keys(cfg.instruments)
    # The web server starts even without a token — the user connects Upstox
    # from the page. Any saved/env token is picked up automatically.
    token = auth.stored_token()
    api = UpstoxAPI(token)
    if not token:
        print("No Upstox token yet — open the dashboard and click 'Connect Upstox'.")
    run_web(cfg, api, token)


def cmd_backtest(args: argparse.Namespace) -> None:
    from .backtest import run_backtest
    from .keyresolver import resolve_index_keys

    cfg = load_config(args.config)
    resolve_index_keys(cfg.instruments)
    api = UpstoxAPI(auth.load_token())
    run_backtest(cfg, api, days=args.days)


def cmd_status(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    try:
        with open(cfg.paper_state_file, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        print("No paper state yet — run `python -m nsemomentum run --mode paper` first.")
        return
    print(f"Paper cash          : {state.get('cash', 0):,.2f}")
    print(f"Realized PnL ({state.get('pnl_date', '-')}): {state.get('realized_pnl_today', 0):,.2f}")
    positions = state.get("positions") or {}
    if not positions:
        print("Open positions      : none")
    else:
        print("Open positions:")
        for pid, pos in positions.items():
            print(
                f"  {pid:<20} {pos['direction']:<5} {pos['symbol']:<24} "
                f"qty={pos['qty']} entry={pos['entry_price']:.2f} at {pos['entry_time']}"
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="nsemomentum",
        description="NSE Momentum — intraday F&O options momentum + Ichimoku strategy on Upstox (paper & live).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="run the Upstox OAuth login flow and save the access token")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("instruments", help="search the Upstox NSE instrument master (verify index keys)")
    p.add_argument("--search", default="", help="substring to search for")
    p.add_argument("--index-only", action="store_true", help="only NSE_INDEX segment rows")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_instruments)

    p = sub.add_parser("run", help="run the trading engine")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--mode", choices=["paper", "live"], help="override mode from config")
    p.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("web", help="serve the NSE Momentum web app (F&O scanner home + Ichimoku dashboard)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--host", default=None, help="bind address (default from config)")
    p.add_argument("--port", type=int, default=None, help="port (default from config)")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("backtest", help="replay historical candles through the strategy")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--days", type=int, default=10)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("status", help="show paper account state")
    p.add_argument("--config", default="config.yaml")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
