from datetime import date, datetime, time, timedelta, timezone

from nsemomentum.fno_universe import build_fno_universe
from nsemomentum.momentum_web import next_scan_at, scan_due, symbol_score

IST = timezone(timedelta(hours=5, minutes=30))
TODAY = date(2026, 8, 3)


# ---------------------------------------------------------- universe


def _ms(d: date) -> int:
    """date -> epoch millis (UTC midnight), the Upstox expiry format."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def sample_rows():
    near = TODAY + timedelta(days=20)
    far = TODAY + timedelta(days=48)
    past = TODAY - timedelta(days=5)
    return [
        # RELIANCE stock futures: near + far month -> near-month wins
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_key": "NSE_EQ|INE002A01018",
         "underlying_symbol": "RELIANCE", "instrument_key": "NSE_FO|RIL_NEAR", "expiry": _ms(near)},
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_key": "NSE_EQ|INE002A01018",
         "underlying_symbol": "RELIANCE", "instrument_key": "NSE_FO|RIL_FAR", "expiry": _ms(far)},
        # INFY futures with an ISO expiry string
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_key": "NSE_EQ|INE009A01021",
         "underlying_symbol": "INFY", "instrument_key": "NSE_FO|INFY_NEAR", "expiry": near.isoformat()},
        # NIFTY index future -> excluded (underlying is an index, not equity)
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_key": "NSE_INDEX|Nifty 50",
         "underlying_symbol": "NIFTY", "instrument_key": "NSE_FO|NIFTY", "expiry": _ms(near)},
        # a stock option -> not a FUT, ignored
        {"segment": "NSE_FO", "instrument_type": "CE", "underlying_key": "NSE_EQ|INE002A01018",
         "underlying_symbol": "RELIANCE", "instrument_key": "NSE_FO|RIL_CE", "expiry": _ms(near)},
        # an expired stock future -> excluded
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_key": "NSE_EQ|INE669E01016",
         "underlying_symbol": "OLDCO", "instrument_key": "NSE_FO|OLD", "expiry": _ms(past)},
        # equity + index rows -> ignored
        {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "RELIANCE",
         "instrument_key": "NSE_EQ|INE002A01018"},
    ]


def test_build_fno_universe_picks_near_month_stock_futures():
    uni = build_fno_universe(rows=sample_rows(), today=TODAY)
    names = [s.name for s in uni]
    assert names == ["INFY", "RELIANCE"]  # sorted, index + expired excluded
    ril = next(s for s in uni if s.name == "RELIANCE")
    assert ril.key == "NSE_EQ|INE002A01018"
    assert ril.futures_key == "NSE_FO|RIL_NEAR"  # near month, not far


def test_build_fno_universe_empty_when_no_stock_futures():
    rows = [{"segment": "NSE_INDEX", "instrument_type": "FUT", "underlying_key": "NSE_INDEX|Nifty 50"}]
    assert build_fno_universe(rows=rows, today=TODAY) == []


# ---------------------------------------------------------- ranking


def _pipe(action="BUY_CE", screen=False, ready=False, conv=0.0):
    return {"action": action, "screen_passed": screen, "trade_ready": ready, "conviction": conv}


def test_symbol_score_orders_ready_above_screened_above_rest():
    ready = symbol_score([_pipe(ready=True, screen=True, conv=1.0)])
    screened = symbol_score([_pipe(screen=True, conv=1.0)])
    directional = symbol_score([_pipe(conv=0.5)])
    avoid = symbol_score([_pipe(action="AVOID")])
    assert ready > screened > directional > avoid


def test_symbol_score_takes_best_timeframe():
    s = symbol_score([_pipe(action="AVOID"), _pipe(ready=True, screen=True, conv=1.0)])
    assert s == symbol_score([_pipe(ready=True, screen=True, conv=1.0)])


# ------------------------------------------------------- scheduling


SCAN_TIMES = [time(9, 15), time(13, 0)]


def test_scan_due_fires_after_each_scan_time_once():
    # before the open -> not due
    assert scan_due(datetime(2026, 8, 3, 9, 0, tzinfo=IST), SCAN_TIMES, None) is False
    # just after 09:15, never scanned -> due
    assert scan_due(datetime(2026, 8, 3, 9, 16, tzinfo=IST), SCAN_TIMES, None) is True
    # already scanned at 09:16, now 12:00 -> not due again until 13:00
    last = datetime(2026, 8, 3, 9, 16, tzinfo=IST)
    assert scan_due(datetime(2026, 8, 3, 12, 0, tzinfo=IST), SCAN_TIMES, last) is False
    # now past 13:00 with the morning scan as last -> due again
    assert scan_due(datetime(2026, 8, 3, 13, 1, tzinfo=IST), SCAN_TIMES, last) is True


def test_next_scan_at():
    n = next_scan_at(datetime(2026, 8, 3, 8, 0, tzinfo=IST), SCAN_TIMES)
    assert (n.hour, n.minute, n.day) == (9, 15, 3)
    n = next_scan_at(datetime(2026, 8, 3, 10, 0, tzinfo=IST), SCAN_TIMES)
    assert (n.hour, n.minute, n.day) == (13, 0, 3)
    n = next_scan_at(datetime(2026, 8, 3, 14, 0, tzinfo=IST), SCAN_TIMES)
    assert (n.hour, n.minute, n.day) == (9, 15, 4)  # tomorrow
