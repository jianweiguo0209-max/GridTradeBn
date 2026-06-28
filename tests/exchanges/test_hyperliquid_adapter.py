from tests.exchanges.test_ccxt_adapter import FakeCcxtClient


def _hl():
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    return HyperliquidAdapter(FakeCcxtClient())


def test_symbol_mapping_roundtrip():
    a = _hl()
    assert a.to_native('BTC/USDT:USDT') == 'BTC/USDC:USDC'
    assert a.to_canonical('BTC/USDC:USDC') == 'BTC/USDT:USDT'


def test_funding_interval_and_name():
    assert _hl().FUNDING_INTERVAL_HOURS == 1
    assert _hl().name == 'hyperliquid'


def test_from_credentials_builds_ccxt_client():
    import ccxt
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    a = HyperliquidAdapter.from_credentials('0xWALLET', '0xKEY')
    assert isinstance(a.client, ccxt.hyperliquid)
