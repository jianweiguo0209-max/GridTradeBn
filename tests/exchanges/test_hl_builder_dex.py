# tests/exchanges/test_hl_builder_dex.py
"""HL builder-dex(HIP-3) 信息面适配：openOrders/positions 对 builder 资产需带 dex 参数
（mainnet 2026-07-06 KIOXIA 实证：action 跨 dex、info 默认只查主 dex → fuse 每轮误判
被丢重挂堆积 166 张孤儿触发单）。主 dex 路径必须逐字节不变（差分守卫）。"""
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter

BTC = 'BTC/USDC:USDC'
KIO = 'XYZ-KIOXIA/USDC:USDC'

_MARKETS = {
    BTC: {'symbol': BTC, 'swap': True, 'base': 'BTC', 'info': {'name': 'BTC'}},
    KIO: {'symbol': KIO, 'swap': True, 'base': 'XYZ-KIOXIA',
          'info': {'name': 'xyz:KIOXIA', 'dex': 'xyz', 'hip3': True}},
}


class _Client:
    markets = _MARKETS
    walletAddress = '0xW'

    def __init__(self):
        self.calls = []

    def load_markets(self):
        return self.markets

    def fetch_open_orders(self, symbol=None):
        self.calls.append(('ccxt_open_orders', symbol))
        return [{'id': 'm1', 'symbol': BTC, 'side': 'buy', 'price': 50000.0,
                 'amount': 0.1, 'filled': 0.0, 'status': 'open', 'info': {}}]

    def fetch_positions(self, symbols=None, params=None):
        self.calls.append(('ccxt_positions', symbols))
        return [{'symbol': BTC, 'contracts': 0.5, 'side': 'long', 'entryPrice': 50000.0}]

    def publicPostInfo(self, params):
        self.calls.append(('info', params.get('type'), params.get('dex')))
        t = params.get('type')
        if t == 'frontendOpenOrders' and params.get('dex') == 'xyz':
            return [
                {'coin': 'xyz:KIOXIA', 'side': 'A', 'limitPx': '570.6', 'sz': '0.071',
                 'oid': 111, 'isTrigger': False, 'reduceOnly': False},
                {'coin': 'xyz:KIOXIA', 'side': 'B', 'limitPx': '293.29', 'sz': '2.054',
                 'oid': 222, 'isTrigger': True, 'reduceOnly': True},
            ]
        if t == 'clearinghouseState' and params.get('dex') == 'xyz':
            return {'assetPositions': [
                {'position': {'coin': 'xyz:KIOXIA', 'szi': '0.071', 'entryPx': '560.0'}}]}
        if t == 'orderStatus':
            oid = params.get('oid')
            if oid == 111:
                return {'status': 'order', 'order': {'status': 'open', 'order': {}}}
            if oid == 222:
                return {'status': 'order', 'order': {'status': 'filled', 'order': {}}}
            if oid == 333:
                return {'status': 'order', 'order': {'status': 'canceled', 'order': {}}}
            return {'status': 'unknownOid'}
        return []


def _ad():
    return HyperliquidAdapter(_Client())


def test_main_dex_open_orders_path_unchanged():
    # 差分守卫：主 dex 币走 ccxt 原路径，不发任何带 dex 的 info 请求
    ad = _ad()
    out = ad.fetch_open_orders(BTC)
    assert [o.id for o in out] == ['m1']
    assert ('ccxt_open_orders', BTC) in ad.client.calls
    assert not any(c[0] == 'info' and c[1] == 'frontendOpenOrders' for c in ad.client.calls)


def test_builder_open_orders_via_dex_param_with_side_mapping():
    ad = _ad()
    out = ad.fetch_open_orders(KIO)
    assert ('info', 'frontendOpenOrders', 'xyz') in ad.client.calls
    assert {(o.id, o.side) for o in out} == {('111', 'sell'), ('222', 'buy')}   # A=卖/B=买
    assert all(o.symbol == KIO for o in out)


def test_open_orders_all_merges_main_and_builder_dex():
    ad = _ad()
    out = ad.fetch_open_orders_all([BTC, KIO])
    ids = {o.id for o in out}
    assert ids == {'m1', '111', '222'}
    assert ('ccxt_open_orders', None) in ad.client.calls          # 主 dex 账户级
    assert ('info', 'frontendOpenOrders', 'xyz') in ad.client.calls


def test_builder_positions_via_dex_param():
    ad = _ad()
    p = ad.fetch_positions(KIO)
    assert ('info', 'clearinghouseState', 'xyz') in ad.client.calls
    assert p.symbol == KIO and p.net_size == 0.071 and p.avg_price == 560.0
    allp = ad.fetch_positions_all([BTC, KIO])
    assert allp == {BTC: 0.5, KIO: 0.071}


def test_order_status_mapping():
    ad = _ad()
    assert ad.order_status(KIO, '111') == 'open'
    assert ad.order_status(KIO, '222') == 'filled'
    assert ad.order_status(KIO, '333') == 'canceled'
    assert ad.order_status(KIO, '999') == 'unknown'


def test_builder_prices_via_dex_allmids_not_ticker():
    # builder 缺价回退用 dex 版 allMids（0.1s），勿用 fetchTicker（实测 10s/次，
    # 曾把 mainnet 轮长从 2.4s 拖到 13.6s）。
    class _ClientP(_Client):
        def publicPostInfo(self, params):
            self.calls.append(('info', params.get('type'), params.get('dex')))
            t = params.get('type')
            if t == 'allMids' and params.get('dex') == 'xyz':
                return {'xyz:KIOXIA': '571.2'}
            if t == 'allMids':
                return {'BTC': '50000.5'}
            return _Client.publicPostInfo(self, params)
        def fetch_ticker(self, symbol):
            raise AssertionError('builder 价不得走 fetchTicker（10s 慢路径）')

    ad = HyperliquidAdapter(_ClientP())
    out = ad.fetch_prices_all([BTC, KIO])
    assert out == {BTC: 50000.5, KIO: 571.2}
    assert ('info', 'allMids', 'xyz') in ad.client.calls
    assert ad.fetch_price(KIO) == 571.2                    # 单币路径同样走 dex allMids
