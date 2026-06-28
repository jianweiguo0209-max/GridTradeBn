from gridtrade.exchanges.hyperliquid import HyperliquidAdapter


class _Client:
    """最小 ccxt 客户端桩：记录 create_order 入参。"""
    def __init__(self):
        self.calls = []

    def fetch_ticker(self, sym):
        return {'last': 123.0}

    def create_order(self, sym, typ, side, size, price, params):
        self.calls.append((sym, typ, side, size, price, params))
        return {'id': '1', 'symbol': sym, 'side': side, 'price': price or 0.0,
                'amount': size, 'filled': size, 'status': 'closed', 'info': {}}


def test_hl_market_order_passes_reference_price_for_slippage():
    c = _Client()
    ad = HyperliquidAdapter(c)
    ad.create_market_order('BTC/USDT:USDT', 'buy', 0.5, client_oid='x')
    sym, typ, side, size, price, params = c.calls[0]
    assert typ == 'market'
    assert price == 123.0          # 传了当前价（HL 算滑点上限用），不是 None
    assert sym == 'BTC/USDC:USDC'  # 规范化到 HL 原生符号


def test_hl_limit_order_unchanged_passes_explicit_price():
    c = _Client()
    ad = HyperliquidAdapter(c)
    ad.create_limit_order('ETH/USDT:USDT', 'sell', 200.0, 1.0, client_oid='y')
    sym, typ, side, size, price, params = c.calls[0]
    assert typ == 'limit' and price == 200.0
