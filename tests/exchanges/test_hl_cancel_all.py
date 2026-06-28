from gridtrade.exchanges.hyperliquid import HyperliquidAdapter


class _Client:
    def __init__(self):
        self.canceled = []

    def fetch_open_orders(self, sym):
        return [{'id': '1', 'symbol': sym, 'side': 'buy', 'price': 1.0, 'amount': 1.0,
                 'filled': 0.0, 'status': 'open', 'info': {}},
                {'id': '2', 'symbol': sym, 'side': 'sell', 'price': 2.0, 'amount': 1.0,
                 'filled': 0.0, 'status': 'open', 'info': {}}]

    def cancel_order(self, order_id, sym):
        self.canceled.append(order_id)


def test_hl_cancel_all_cancels_each_open_order():
    # HL 无 cancelAllOrders -> 逐个撤
    c = _Client()
    HyperliquidAdapter(c).cancel_all('BTC/USDT:USDT')
    assert sorted(c.canceled) == ['1', '2']
