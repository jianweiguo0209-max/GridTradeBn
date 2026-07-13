import ccxt


def test_ccxt_has_binanceusdm():
    assert hasattr(ccxt, 'binanceusdm'), 'ccxt 缺少 binanceusdm 类（需升级）'


def test_unified_methods_present():
    ex = ccxt.binanceusdm({'enableRateLimit': True})
    for m in ('fetch_ohlcv', 'create_order', 'cancel_order',
              'fetch_open_orders', 'fetch_balance', 'fetch_positions',
              'set_leverage', 'load_markets', 'fetch_funding_rate_history'):
        assert hasattr(ex, m), f'binanceusdm 缺少统一方法 {m}'
