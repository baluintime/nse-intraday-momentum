from datetime import datetime, timedelta
from unittest import mock

import pytest
from zoneinfo import ZoneInfoNotFoundError

from nsemomentum import tzutil


def test_get_zone_normal():
    tz = tzutil.get_zone("Asia/Kolkata")
    assert tz.utcoffset(datetime(2026, 7, 16)) == timedelta(hours=5, minutes=30)


def test_get_zone_falls_back_without_tzdata():
    with mock.patch.object(tzutil, "ZoneInfo", side_effect=ZoneInfoNotFoundError("x")):
        tz = tzutil.get_zone("Asia/Kolkata")
        assert tz.utcoffset(datetime(2026, 7, 16)) == timedelta(hours=5, minutes=30)


def test_get_zone_unknown_key_still_raises():
    with mock.patch.object(tzutil, "ZoneInfo", side_effect=ZoneInfoNotFoundError("x")):
        with pytest.raises(ZoneInfoNotFoundError):
            tzutil.get_zone("Mars/Olympus_Mons")
