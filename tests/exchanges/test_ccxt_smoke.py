import ccxt


def test_ccxt_has_okx_and_hyperliquid():
    assert hasattr(ccxt, 'okx'), 'ccxt 缺少 okx 类'
    assert hasattr(ccxt, 'hyperliquid'), 'ccxt 版本过低，无 hyperliquid（需升级）'


def test_unified_methods_present():
    for name in ('okx', 'hyperliquid'):
        cls = getattr(ccxt, name)
        ex = cls({'enableRateLimit': True})
        for m in ('fetch_ohlcv', 'create_order', 'cancel_order',
                  'fetch_open_orders', 'fetch_balance', 'fetch_positions',
                  'set_leverage', 'load_markets', 'fetch_funding_rate_history'):
            assert hasattr(ex, m), f'{name} 缺少统一方法 {m}'
