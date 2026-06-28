from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter


class _Client:
    def __init__(self): self.calls = []
    def fetch_ticker(self, s): return {'last': 100.0}
    def create_order(self, sym, typ, side, size, price, params):
        self.calls.append(params)
        return {'id': '1', 'symbol': sym, 'side': side, 'price': price or 0.0,
                'amount': size, 'filled': 0.0, 'status': 'open', 'info': {}}


def test_default_encode_cloid_is_identity():
    assert FakeExchange().encode_cloid('g:1:0') == 'g:1:0'


def test_hl_encode_cloid_returns_none():
    assert HyperliquidAdapter(_Client()).encode_cloid('g:1:0') is None


def test_hl_create_order_omits_client_order_id():
    c = _Client()
    HyperliquidAdapter(c).create_limit_order('BTC/USDT:USDT', 'buy', 100.0, 1.0,
                                             client_oid='g:1:0')
    assert 'clientOrderId' not in c.calls[0]    # HL 不发非法 cloid
