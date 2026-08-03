from datetime import datetime, timedelta, timezone

from nsemomentum.candles import Candle, CandleSeries, TimeframeAggregator

IST = timezone(timedelta(hours=5, minutes=30))


def c(minute: int, o=100.0, h=101.0, low=99.0, close=100.5, hour=9, vol=10.0) -> Candle:
    return Candle(datetime(2026, 7, 16, hour, minute, tzinfo=IST), o, h, low, close, vol)


def test_series_dedupes_and_orders():
    s = CandleSeries()
    assert s.append(c(15))
    assert not s.append(c(15))  # duplicate ts rejected
    assert not s.append(c(14))  # older rejected
    assert s.append(c(16))
    assert len(s) == 2


def test_from_upstox_row():
    row = ["2026-07-16T09:15:00+05:30", 100, 101, 99, 100.5, 1234, 0]
    candle = Candle.from_upstox(row)
    assert candle.ts.hour == 9 and candle.ts.minute == 15
    assert candle.close == 100.5 and candle.volume == 1234


def test_five_min_aggregation():
    agg = TimeframeAggregator(5)
    # 09:15..09:19 -> one 5m bucket starting 09:15
    out = []
    for m, (o, h, low, cl) in zip(
        range(15, 20),
        [(100, 102, 99, 101), (101, 103, 100, 102), (102, 104, 98, 99), (99, 100, 97, 98), (98, 105, 96, 104)],
    ):
        out.append(agg.feed(Candle(datetime(2026, 7, 16, 9, m, tzinfo=IST), o, h, low, cl, 1)))
    assert all(x is None for x in out)  # bucket not complete yet

    done = agg.feed(c(20))  # first candle of the next bucket completes 09:15
    assert done is not None
    assert done.ts.minute == 15
    assert done.open == 100 and done.high == 105 and done.low == 96 and done.close == 104
    assert done.volume == 5

    tail = agg.flush()
    assert tail is not None and tail.ts.minute == 20


def test_aggregation_across_day_gap():
    agg = TimeframeAggregator(5)
    assert agg.feed(Candle(datetime(2026, 7, 15, 15, 29, tzinfo=IST), 1, 2, 0.5, 1.5, 1)) is None
    done = agg.feed(Candle(datetime(2026, 7, 16, 9, 15, tzinfo=IST), 5, 6, 4, 5.5, 1))
    assert done is not None and done.ts == datetime(2026, 7, 15, 15, 25, tzinfo=IST)
