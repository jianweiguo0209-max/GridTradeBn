from tests.exchanges.test_ccxt_adapter import FakeCcxtClient


def _hl():
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    return HyperliquidAdapter(FakeCcxtClient())


def test_symbol_mapping_roundtrip():
    a = _hl()
    # canonical 如实反映 HL 结算币 USDC（不再伪装成 USDT）
    assert a.to_canonical('BTC/USDC:USDC') == 'BTC/USDC:USDC'
    assert a.to_native('BTC/USDC:USDC') == 'BTC/USDC:USDC'
    assert a.to_native('ETH/USDC:USDC') == 'ETH/USDC:USDC'


def test_canonical_derives_from_quote_currency_override():
    # 实例覆写 quote_currency -> 符号随之派生（单一事实源）
    a = _hl()
    a.quote_currency = 'USDT'
    assert a.to_canonical('BTC/USDC:USDC') == 'BTC/USDT:USDT'
    assert a.to_native('BTC/USDT:USDT') == 'BTC/USDT:USDT'


def test_funding_interval_and_name():
    assert _hl().FUNDING_INTERVAL_HOURS == 1
    assert _hl().name == 'hyperliquid'


def test_from_credentials_builds_ccxt_client():
    import ccxt
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    a = HyperliquidAdapter.from_credentials('0xWALLET', '0xKEY')
    assert isinstance(a.client, ccxt.hyperliquid)
