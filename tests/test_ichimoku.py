from nsemomentum.ichimoku import (
    IchimokuParams,
    compute_state,
    long_entry,
    long_exit,
    short_entry,
    short_exit,
)

# Small periods so the arithmetic is hand-checkable.
P = IchimokuParams(tenkan=2, kijun=3, senkou_b=4, displacement=2)


def test_min_candles():
    assert P.min_candles == 6
    highs = [10.0] * 5
    lows = [9.0] * 5
    assert compute_state(highs, lows, P) is None


def test_compute_state_hand_checked():
    #            0    1    2    3    4    5
    highs = [10.0, 12.0, 11.0, 13.0, 14.0, 15.0]
    lows = [9.0, 10.0, 10.5, 11.0, 12.0, 13.0]
    s = compute_state(highs, lows, P)
    assert s is not None
    # current index i=5: tenkan over idx 4..5, kijun over idx 3..5
    assert s.tenkan == (15.0 + 12.0) / 2  # 13.5
    assert s.kijun == (15.0 + 11.0) / 2  # 13.0
    # displayed spans come from j = 5 - 2 = 3
    tenkan_j = (13.0 + 10.5) / 2  # idx 2..3 -> 11.75
    kijun_j = (13.0 + 10.0) / 2  # idx 1..3 -> 11.5
    assert s.span_a == (tenkan_j + kijun_j) / 2  # 11.625
    assert s.span_b == (13.0 + 9.0) / 2  # idx 0..3 -> 11.0
    assert s.cloud_top == 11.625
    assert s.cloud_bottom == 11.0


def test_entry_exit_rules():
    s = compute_state(
        [10.0, 12.0, 11.0, 13.0, 14.0, 15.0], [9.0, 10.0, 10.5, 11.0, 12.0, 13.0], P
    )
    # levels: tenkan 13.5, kijun 13.0, span_a 11.625, span_b 11.0
    assert long_entry(14.0, s)  # above everything
    assert not long_entry(13.2, s)  # below tenkan -> not above ALL
    assert not long_exit(14.0, s)
    assert long_exit(13.2, s)  # below tenkan (any one level) -> exit
    assert short_entry(10.5, s)  # below everything
    assert not short_entry(11.5, s)  # above span_b
    assert short_exit(11.5, s)  # above any one level -> exit
    assert not short_exit(10.5, s)


def test_strictness_at_equality():
    s = compute_state(
        [10.0, 12.0, 11.0, 13.0, 14.0, 15.0], [9.0, 10.0, 10.5, 11.0, 12.0, 13.0], P
    )
    # close exactly ON the highest level (tenkan 13.5): not strictly above all,
    # and not strictly below any -> neither entry nor exit triggers
    assert not long_entry(13.5, s)
    assert not long_exit(13.5, s)
    # close exactly ON the lowest level (span_b 11.0): not strictly below all,
    # but strictly above no level either -> no short entry, no short exit
    assert not short_entry(11.0, s)
    assert not short_exit(11.0, s)
