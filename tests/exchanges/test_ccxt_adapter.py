import pandas as pd


class FakeCcxtClient:
    """最小 ccxt-like 桩：只实现 CcxtAdapter 用到的方法。"""
    def __init__(self):
        self.created = []
        self.canceled = []
    def parse_timeframe(self, tf):
        return 3600
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None, params=None):
        # ccxt: [ms, open, high, low, close, volume]
        return [[1704067200000, 1.0, 2.0, 0.5, 1.5, 10.0],
                [1704070800000, 1.5, 2.5, 1.0, 2.0, 20.0]]
    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        return [{'timestamp': 1704067200000, 'fundingRate': 0.0001},
                {'timestamp': 1704070800000, 'fundingRate': -0.0002}]
    def fetch_ticker(self, symbol):
        return {'last': 2.0}
    def fetch_tickers(self, symbols=None, params=None):
        return {
            'BTC/USDT:USDT': {'quoteVolume': 1000.0},
            'ETH/USDT:USDT': {'quoteVolume': 500.0},
            'NOVOL/USDT:USDT': {'quoteVolume': None},   # 无量 → 跳过
        }
    def fetch_balance(self, params=None):
        return {'USDT': {'total': 1000.0, 'free': 800.0}}
    def fetch_positions(self, symbols=None, params=None):
        return [{'symbol': 'BTC/USDT:USDT', 'contracts': 3.0, 'side': 'long',
                 'entryPrice': 100.0}]
    def create_order(self, symbol, type, side, amount, price=None, params=None):
        oid = str(len(self.created) + 1)
        self.created.append((symbol, type, side, amount, price, params))
        return {'id': oid, 'clientOrderId': (params or {}).get('clientOrderId', oid),
                'symbol': symbol, 'side': side, 'price': price or 0.0, 'amount': amount,
                'filled': 0.0, 'status': 'open'}
    def cancel_order(self, id, symbol=None, params=None):
        self.canceled.append((id, symbol))
    def cancel_all_orders(self, symbol=None, params=None):
        self.canceled.append(('ALL', symbol))
    def fetch_open_orders(self, symbol=None, params=None):
        return [{'id': '7', 'clientOrderId': 'g:0', 'symbol': symbol, 'side': 'buy',
                 'price': 1.0, 'amount': 2.0, 'filled': 0.0, 'status': 'open'}]
    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        return [{'id': 't1', 'order': 'o1', 'symbol': symbol, 'side': 'buy',
                 'price': 1.0, 'amount': 2.0, 'timestamp': 1704067200000,
                 'fee': {'cost': 0.1}, 'info': {'clOrdId': 'g:0'}}]
    def set_leverage(self, leverage, symbol=None, params=None):
        self._lev = (leverage, symbol)
    def load_markets(self):
        return {'BTC/USDT:USDT': {}}
    markets = {'BTC/USDT:USDT': {'swap': True, 'precision': {'price': 0.1, 'amount': 0.001},
                                 'limits': {'amount': {'min': 0.001}},
                                 'active': True, 'info': {'listTime': '0'}}}


def _adapter():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    return CcxtAdapter(FakeCcxtClient(), name='ccxt')


def test_fetch_ohlcv_maps_to_candle_cols():
    from gridtrade.exchanges.base import CANDLE_COLS
    df = _adapter().fetch_ohlcv('BTC/USDT:USDT', '1H', 0, 10**13)
    assert list(df.columns) == CANDLE_COLS
    assert df['close'].tolist() == [1.5, 2.0]
    assert df['candle_begin_time'].iloc[0] == pd.Timestamp('2024-01-01 00:00:00')


def test_fetch_ohlcv_quote_volume_uses_midprice_not_close():
    # quote_volume = (open+close)/2 * vol（legacy 文档化回退），volCcy = vol。
    # 关键：vwap = quote_volume/volCcy = (open+close)/2，不得塌成 close（否则 Vwapbias 失真）。
    df = _adapter().fetch_ohlcv('BTC/USDT:USDT', '1H', 0, 10**13)
    # 行0: open=1.0 close=1.5 vol=10 -> (1.0+1.5)/2*10 = 12.5 ；行1: (1.5+2.0)/2*20 = 35.0
    assert df['quote_volume'].tolist() == [12.5, 35.0]
    assert df['volCcy'].tolist() == [10.0, 20.0]
    vwap = (df['quote_volume'] / df['volCcy']).tolist()
    assert vwap == [1.25, 1.75]
    assert vwap != df['close'].tolist()        # vwap 未塌成 close


def test_fetch_funding_history_maps_cols():
    from gridtrade.exchanges.base import FUNDING_COLS
    df = _adapter().fetch_funding_history('BTC/USDT:USDT', 0, 10**13)
    assert list(df.columns) == FUNDING_COLS
    assert df['fundingRate'].tolist() == [0.0001, -0.0002]


def test_balance_and_position_mapping():
    a = _adapter()
    bal = a.fetch_balance()
    assert bal.equity == 1000.0 and bal.cash == 800.0
    pos = a.fetch_positions('BTC/USDT:USDT')
    assert pos.net_size == 3.0 and pos.avg_price == 100.0


def test_create_limit_order_passes_client_oid():
    a = _adapter()
    o = a.create_limit_order('BTC/USDT:USDT', 'buy', 1.0, 2.0, client_oid='g:0')
    assert o.client_oid == 'g:0' and o.status == 'open'
    # client.created 最后一项的 params 应带 clientOrderId
    _, type_, side, amount, price, params = a.client.created[-1]
    assert type_ == 'limit' and params.get('clientOrderId') == 'g:0'


def test_open_orders_and_trades_mapping():
    a = _adapter()
    orders = a.fetch_open_orders('BTC/USDT:USDT')
    assert orders[0].client_oid == 'g:0'
    trades = a.fetch_my_trades('BTC/USDT:USDT')
    assert trades[0].client_oid == 'g:0' and trades[0].fee == 0.1


def test_instruments_mapping():
    a = _adapter()
    insts = a.list_instruments()
    assert insts[0].symbol == 'BTC/USDT:USDT' and insts[0].state == 'live'


def test_list_instruments_swap_only_and_deduped():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter

    class _FoldClient:
        def load_markets(self):
            return self.markets
        markets = {
            'BTC/USDC:USDC':   {'swap': True,  'precision': {'price': 0.1, 'amount': 0.001},
                                'limits': {'amount': {'min': 0.001}}, 'active': True, 'info': {}},
            'BTC/USDC':        {'swap': False, 'spot': True, 'precision': {}, 'limits': {},
                                'active': True, 'info': {}},                       # spot → 丢
            'ETH/USDC:USDC':   {'swap': True,  'precision': {}, 'limits': {}, 'active': True, 'info': {}},
            'ETH/USDC:USDC-2': {'swap': True,  'precision': {}, 'limits': {}, 'active': True, 'info': {}},  # 折叠成 ETH → 去重
            'SOL/USDC':        {'swap': False, 'spot': True, 'precision': {}, 'limits': {},
                                'active': True, 'info': {}},                       # spot-only、无 swap 对应 → 丢（隔离 swap 过滤 vs 去重）
        }

    class _FoldAdapter(CcxtAdapter):
        def to_canonical(self, native):
            return native.split('/')[0] + '/USDC:USDC'

    a = _FoldAdapter(_FoldClient(), name='fold')
    syms = [i.symbol for i in a.list_instruments()]
    assert syms == ['BTC/USDC:USDC', 'ETH/USDC:USDC']   # spot 丢、重复 canonical 去重
    assert 'SOL/USDC:USDC' not in syms                  # SOL 无 swap 对应，若 swap 过滤被删也不会因去重被吸收


def test_fetch_24h_quote_volumes_maps_quotevolume():
    a = _adapter()
    vols = a.fetch_24h_quote_volumes()
    assert vols == {'BTC/USDT:USDT': 1000.0, 'ETH/USDT:USDT': 500.0}   # None 被跳过
