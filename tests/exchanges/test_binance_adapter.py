from tests.exchanges.test_ccxt_adapter import FakeCcxtClient


class FakeBinanceClient(FakeCcxtClient):
    """binanceusdm 桩：在通用 ccxt 桩上补币安原生端点。markets 含 USDT/USDC 双结算。"""
    def __init__(self):
        super().__init__()
        self.pinged = 0
        self.markets = {
            'BTC/USDT:USDT': {'id': 'BTCUSDT', 'symbol': 'BTC/USDT:USDT', 'swap': True,
                              'settle': 'USDT', 'base': 'BTC', 'active': True,
                              'precision': {'price': 0.1, 'amount': 0.001},
                              'limits': {'amount': {'min': 0.001}, 'cost': {'min': 50.0}},
                              'info': {'listTime': '0'}},
            'ETH/USDT:USDT': {'id': 'ETHUSDT', 'symbol': 'ETH/USDT:USDT', 'swap': True,
                              'settle': 'USDT', 'base': 'ETH', 'active': True,
                              'precision': {'price': 0.01, 'amount': 0.01},
                              'limits': {'amount': {'min': 0.01}, 'cost': {'min': 20.0}},
                              'info': {'listTime': '0'}},
            'BTC/USDC:USDC': {'id': 'BTCUSDC', 'symbol': 'BTC/USDC:USDC', 'swap': True,
                              'settle': 'USDC', 'base': 'BTC', 'active': True,
                              'precision': {'price': 0.1, 'amount': 0.001},
                              'limits': {'amount': {'min': 0.001}, 'cost': {'min': 5.0}},
                              'info': {'listTime': '0'}},
        }
    def load_markets(self):
        return self.markets
    def fapiPublicGetPing(self, params=None):
        self.pinged += 1
        return {}
    def fapiPublicGetKlines(self, params=None):
        self.kline_calls = getattr(self, 'kline_calls', [])
        self.kline_calls.append(dict(params or {}))
        # 原生 12 列（数值为字符串——忠实币安响应）
        return [
            [1704067200000, "1.0", "2.0", "0.5", "1.5", "10.0", 1704070799999,
             "13.7", 5, "4.0", "5.5", "0"],
            [1704070800000, "1.5", "2.5", "1.0", "2.0", "20.0", 1704074399999,
             "36.2", 8, "9.0", "16.3", "0"],
        ]


def _binance(client=None):
    from gridtrade.exchanges.binance import BinanceAdapter
    return BinanceAdapter(client or FakeBinanceClient())


def test_basic_attrs():
    a = _binance()
    assert a.name == 'binance'
    assert a.quote_currency == 'USDT'
    assert a.FUNDING_INTERVAL_HOURS == 8
    # ccxt 统一符号即规范符号：恒等映射
    assert a.to_native('BTC/USDT:USDT') == 'BTC/USDT:USDT'
    assert a.to_canonical('BTC/USDT:USDT') == 'BTC/USDT:USDT'


def test_list_instruments_filters_settle():
    # fapi 同时挂 USDT-M 与 USDC-M：只收本结算币（spec §3.1）
    syms = [i.symbol for i in _binance().list_instruments()]
    assert 'BTC/USDT:USDT' in syms and 'ETH/USDT:USDT' in syms
    assert 'BTC/USDC:USDC' not in syms


def test_encode_cloid_legal_passthrough():
    a = _binance()
    # 内部三种格式均在币安 futures 合法字符集内（含 ':'）→ 原样直传
    for oid in ('12:3:1', '12:fuse:low', '12:close:2'):
        assert a.encode_cloid(oid) == oid
    assert a.encode_cloid(None) is None


def test_encode_cloid_sanitizes_and_rejects_overlong():
    import pytest
    a = _binance()
    assert a.encode_cloid('a b中') == 'a-b-'       # 非法字符确定性替换 '-'
    with pytest.raises(ValueError):
        a.encode_cloid('x' * 37)                    # 超长断言防越界（spec §5.1）


def test_exchange_status_ping():
    c = FakeBinanceClient()
    a = _binance(c)
    assert a.exchange_status() == 'ok' and c.pinged == 1
    def boom(params=None):
        raise RuntimeError('down')
    c.fapiPublicGetPing = boom
    assert a.exchange_status() == 'maintenance'


def test_from_credentials_testnet_sandbox():
    import ccxt
    from gridtrade.exchanges.binance import BinanceAdapter
    a = BinanceAdapter.from_credentials('k', 's', testnet=True)
    assert isinstance(a.client, ccxt.binanceusdm)
    # sandbox 模式生效：api url 指向 testnet
    assert 'testnet' in str(a.client.urls['api']).lower()


def test_fetch_ohlcv_real_quote_volume():
    from gridtrade.exchanges.base import CANDLE_COLS
    c = FakeBinanceClient()
    a = _binance(c)
    df = a.fetch_ohlcv('BTC/USDT:USDT', '1h', 0, 10**13)
    assert list(df.columns) == CANDLE_COLS
    # 真实 quote_volume（第8列），非 (open+close)/2*vol 估算（spec §5.4）
    assert df['quote_volume'].tolist() == [13.7, 36.2]
    assert df['volCcy'].tolist() == [10.0, 20.0]
    assert df['close'].tolist() == [1.5, 2.0]
    # 原生 id + interval 直传
    assert c.kline_calls[0]['symbol'] == 'BTCUSDT'
    assert c.kline_calls[0]['interval'] == '1h'
    assert c.kline_calls[0]['limit'] == 1500


def test_fetch_ohlcv_empty():
    c = FakeBinanceClient()
    c.fapiPublicGetKlines = lambda params=None: []
    df = _binance(c).fetch_ohlcv('BTC/USDT:USDT', '1h', 0, 10**13)
    assert df.empty
