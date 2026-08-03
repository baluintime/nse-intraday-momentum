"""Candle primitives: parsing, rolling series, and 1m -> 5m aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    ts: datetime  # start time of the interval (tz-aware)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float = 0.0  # open interest at the candle close (F&O instruments only)

    @staticmethod
    def from_upstox(row: list) -> "Candle":
        """row = [iso_ts, open, high, low, close, volume, oi]"""
        return Candle(
            ts=datetime.fromisoformat(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
            oi=float(row[6]) if len(row) > 6 and row[6] is not None else 0.0,
        )


class CandleSeries:
    """Rolling, time-ordered series of completed candles."""

    def __init__(self, max_len: int = 2000):
        self.max_len = max_len
        self.candles: list[Candle] = []

    def __len__(self) -> int:
        return len(self.candles)

    def last_ts(self) -> datetime | None:
        return self.candles[-1].ts if self.candles else None

    def append(self, candle: Candle) -> bool:
        """Append if strictly newer than the latest candle. Returns True if added."""
        if self.candles and candle.ts <= self.candles[-1].ts:
            return False
        self.candles.append(candle)
        if len(self.candles) > self.max_len:
            del self.candles[: len(self.candles) - self.max_len]
        return True

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self.candles]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self.candles]

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]


class TimeframeAggregator:
    """Aggregates 1-minute candles into N-minute candles.

    A bucket is emitted as completed when a 1m candle belonging to a *later*
    bucket arrives (or on flush()).
    """

    def __init__(self, minutes: int):
        self.minutes = minutes
        self._bucket_ts: datetime | None = None
        self._o = self._h = self._l = self._c = 0.0
        self._v = 0.0

    def _bucket_start(self, ts: datetime) -> datetime:
        return ts.replace(minute=ts.minute - ts.minute % self.minutes, second=0, microsecond=0)

    def feed(self, candle: Candle) -> Candle | None:
        """Feed a completed 1m candle; returns a completed N-minute candle or None."""
        bucket = self._bucket_start(candle.ts)
        completed: Candle | None = None
        if self._bucket_ts is not None and bucket != self._bucket_ts:
            completed = self._emit()
        if self._bucket_ts is None or bucket != self._bucket_ts:
            self._bucket_ts = bucket
            self._o, self._h, self._l, self._c = candle.open, candle.high, candle.low, candle.close
            self._v = candle.volume
        else:
            self._h = max(self._h, candle.high)
            self._l = min(self._l, candle.low)
            self._c = candle.close
            self._v += candle.volume
        return completed

    def flush(self) -> Candle | None:
        """Emit the in-progress bucket (e.g. at session end)."""
        if self._bucket_ts is None:
            return None
        candle = self._emit()
        self._bucket_ts = None
        return candle

    def _emit(self) -> Candle:
        assert self._bucket_ts is not None
        return Candle(self._bucket_ts, self._o, self._h, self._l, self._c, self._v)
