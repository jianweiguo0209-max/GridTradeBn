from gridtrade.exchanges.hyperliquid import HyperliquidAdapter


def _ad():
    return HyperliquidAdapter(None)   # to_native/to_canonical 不用 client


def test_to_canonical_handles_none():
    # HL createOrder 响应不带 symbol -> ccxt 解析出 symbol=None
    ad = _ad()
    assert ad.to_canonical(None) is None
    assert ad.to_canonical('BTC/USDC:USDC') == 'BTC/USDT:USDT'


def test_to_native_handles_none():
    ad = _ad()
    assert ad.to_native(None) is None
    assert ad.to_native('BTC/USDT:USDT') == 'BTC/USDC:USDC'
