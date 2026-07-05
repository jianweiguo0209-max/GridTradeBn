# tests/exchanges/test_hl_account_batch.py
"""HL 账户级批量读：fills/orders/positions 走 symbol=None，allMids 直调，funding 按 delta.coin 分组。"""
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter

BTC = 'BTC/USDC:USDC'
KPEPE = 'KPEPE/USDC:USDC'

_MARKETS = {
    'BTC/USDC:USDC': {'symbol': 'BTC/USDC:USDC', 'swap': True, 'base': 'BTC',
                      'info': {'name': 'BTC'}},
    'KPEPE/USDC:USDC': {'symbol': 'KPEPE/USDC:USDC', 'swap': True, 'base': 'KPEPE',
                        'info': {'name': 'kPEPE'}},   # HL 原生 coin 名小写 k 前缀
}


class _Client:
    markets = _MARKETS

    def __init__(self):
        self.calls = []

    def load_markets(self):
        return self.markets

    def fetch_my_trades(self, symbol, since=None):
        self.calls.append(('fetch_my_trades', symbol, since))
        return [{'id': 't1', 'symbol': KPEPE, 'side': 'buy', 'price': 0.009,
                 'amount': 100.0, 'timestamp': 2000, 'order': 'o1',
                 'fee': {'cost': 0.01}, 'info': {}},
                {'id': 't2', 'symbol': BTC, 'side': 'sell', 'price': 50000.0,
                 'amount': 0.1, 'timestamp': 1000, 'order': 'o2',
                 'fee': {'cost': 0.02}, 'info': {}}]

    def fetch_open_orders(self, symbol=None):
        self.calls.append(('fetch_open_orders', symbol))
        return [{'id': 'o3', 'symbol': BTC, 'side': 'buy', 'price': 49000.0,
                 'amount': 0.1, 'filled': 0.0, 'status': 'open', 'info': {}}]

    def fetch_positions(self, symbols=None, params=None):
        self.calls.append(('fetch_positions', symbols))
        return [{'symbol': KPEPE, 'contracts': 12064.0, 'side': 'short',
                 'entryPrice': 0.0095}]

    def publicPostInfo(self, params):
        self.calls.append(('publicPostInfo', params))
        return {'BTC': '50000.5', 'kPEPE': '0.0091', 'ETH': '3000.0'}

    def fetch_funding_history(self, symbol=None, since=None, limit=None):
        self.calls.append(('fetch_funding_history', symbol, since))
        # HL 实况：账户级全币种 + 查询 symbol 盖印到每行 symbol 字段
        return [{'timestamp': 3000, 'amount': -0.5, 'symbol': symbol,
                 'info': {'delta': {'coin': 'kPEPE'}}},
                {'timestamp': 2500, 'amount': 0.2, 'symbol': symbol,
                 'info': {'delta': {'coin': 'BTC'}}},
                {'timestamp': 100, 'amount': -9.9, 'symbol': symbol,
                 'info': {'delta': {'coin': 'BTC'}}}]     # since 之前 → 应被滤掉


def _ad():
    return HyperliquidAdapter(_Client())


def test_trades_all_symbol_none_and_per_row_mapping():
    ad = _ad()
    out = ad.fetch_my_trades_all([BTC, KPEPE], since_ms=500)
    assert ('fetch_my_trades', None, 500) in ad.client.calls   # 账户级：symbol=None
    assert [t.ts for t in out] == [1000, 2000]                 # 升序
    assert {t.symbol for t in out} == {BTC, KPEPE}             # 逐行真实 symbol


def test_trades_all_filters_unwanted_symbols():
    out = _ad().fetch_my_trades_all([BTC], since_ms=None)
    assert [t.symbol for t in out] == [BTC]                    # KPEPE 行被过滤


def test_open_orders_all_and_positions_all():
    ad = _ad()
    orders = ad.fetch_open_orders_all([BTC, KPEPE])
    assert ('fetch_open_orders', None) in ad.client.calls
    assert [o.id for o in orders] == ['o3']
    pos = ad.fetch_positions_all([BTC, KPEPE])
    assert ('fetch_positions', None) in ad.client.calls
    assert pos == {KPEPE: -12064.0}                            # short → 负；BTC 无仓位行


def test_prices_all_via_allmids_with_coin_mapping():
    out = _ad().fetch_prices_all([BTC, KPEPE])
    assert out == {BTC: 50000.5, KPEPE: 0.0091}                # kPEPE→KPEPE 映射；ETH 不在册被滤


def test_funding_all_grouped_by_delta_coin_pay_positive():
    out = _ad().fetch_funding_payments_all([BTC, KPEPE], since_ms=500)
    assert [p.amount for p in out[KPEPE]] == [0.5]             # 支付为正
    assert [(p.ts, p.amount) for p in out[BTC]] == [(2500, -0.2)]   # ts<since 滤掉
