from nsemomentum.config import IndexConfig
from nsemomentum.keyresolver import resolve_index_keys

MASTER = [
    {"segment": "NSE_INDEX", "instrument_key": "NSE_INDEX|Nifty 50",
     "trading_symbol": "NIFTY 50", "name": "Nifty 50"},
    {"segment": "NSE_INDEX", "instrument_key": "NSE_INDEX|NIFTY MID SELECT",
     "trading_symbol": "NIFTY MID SELECT", "name": "NIFTY Midcap Select"},
    {"segment": "NSE_INDEX", "instrument_key": "NSE_INDEX|NIFTY SMLCAP 50",
     "trading_symbol": "NIFTY SMLCAP 50", "name": "Nifty Smallcap 50"},
    {"segment": "NSE_INDEX", "instrument_key": "NSE_INDEX|Nifty 100",
     "trading_symbol": "NIFTY 100", "name": "Nifty 100"},
    {"segment": "NSE_EQ", "instrument_key": "NSE_EQ|INE002A01018",
     "trading_symbol": "RELIANCE", "name": "Reliance Industries"},
]


def test_valid_key_untouched():
    ix = IndexConfig("NIFTY", "NSE_INDEX|Nifty 50")
    resolve_index_keys([ix], rows=MASTER)
    assert ix.key == "NSE_INDEX|Nifty 50" and ix.enabled


def test_wrong_casing_auto_corrected():
    ix = IndexConfig("SMALLCAP", "NSE_INDEX|Nifty Smlcap 50")
    resolve_index_keys([ix], rows=MASTER)
    assert ix.key == "NSE_INDEX|NIFTY SMLCAP 50" and ix.enabled


def test_smallcap_spelling_alias_resolves():
    ix = IndexConfig("SMALLCAP", "NSE_INDEX|Nifty Smallcap 50")
    resolve_index_keys([ix], rows=MASTER)
    assert ix.key == "NSE_INDEX|NIFTY SMLCAP 50"


def test_resolves_via_name_field():
    # config key totally wrong, but the friendly name matches a master row name
    ix = IndexConfig("MIDCPNIFTY", "NSE_INDEX|bogus", )
    ix.name = "NIFTY Midcap Select"
    resolve_index_keys([ix], rows=MASTER)
    assert ix.key == "NSE_INDEX|NIFTY MID SELECT"


def test_unresolvable_key_disables_index():
    ix = IndexConfig("MYSTERY", "NSE_INDEX|No Such Index")
    resolve_index_keys([ix], rows=MASTER)
    assert ix.enabled is False


def test_disabled_indices_skipped():
    ix = IndexConfig("SMALLCAP", "NSE_INDEX|wrong", enabled=False)
    resolve_index_keys([ix], rows=MASTER)
    assert ix.key == "NSE_INDEX|wrong"  # untouched
