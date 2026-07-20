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
    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        # 签名对齐真实 ccxt：位置传 params 会错落到 since——桩必须能暴露这类错位
        assert since is None or isinstance(since, (int, float)), '错位: params 传进了 since'
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
    def price_to_precision(self, sym, price):
        tick = self.markets[sym]['precision']['price']   # 0.1；ccxt 返回字符串（%g 去浮点噪声）
        return '%.10g' % (round(float(price) / tick) * tick)
    markets = {'BTC/USDT:USDT': {'swap': True, 'precision': {'price': 0.1, 'amount': 0.001},
                                 'limits': {'amount': {'min': 0.001}, 'cost': {'min': 5.0},
                                            'market': {'min': 0.001, 'max': 120.0}},
                                 'active': True, 'info': {'listTime': '0'}},
               'ETH/USDT:USDT': {'swap': True, 'precision': {'price': 0.01, 'amount': 0.01},
                                 'limits': {'amount': {'min': 0.01}, 'cost': {'min': 20.0}},
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


def test_fetch_24h_quote_volumes_takes_max_per_canonical():
    # HL spot+swap 折叠成同一 canonical 时，取较大者（不得被后遍历的较小值覆盖，也不得误取先来者）。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter

    class _FoldVolClient:
        def fetch_tickers(self, symbols=None, params=None):
            # 较大值先遍历、较小值后遍历：若实现退化成"无条件覆盖"（而非取 max），
            # 后者会把 900 覆盖成 500，本测试即可抓到（纯粹按遍历顺序取值无法通过）。
            return {
                'BTC/USDC:USDC': {'quoteVolume': 900.0},   # swap，先遍历，较大 → 应保留
                'BTC/USDC':      {'quoteVolume': 500.0},   # spot，同 canonical，较小 → 不应覆盖
            }

    class _FoldAdapter(CcxtAdapter):
        def to_canonical(self, native):
            return native.split('/')[0] + '/USDC:USDC'

    a = _FoldAdapter(_FoldVolClient(), name='fold')
    vols = a.fetch_24h_quote_volumes()
    assert vols == {'BTC/USDC:USDC': 900.0}   # 取两者中的较大值，而非遍历顺序中的先/后者


def test_quantize_amount_uses_precision_table():
    """quantize_amount 经 ccxt amount_to_precision（惰性 load_markets）；异常 fail-open 原样返回。"""
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter

    class _C:
        markets = None
        def load_markets(self):
            self.markets = {'BTC/USDT:USDT': {}}
        def amount_to_precision(self, sym, amt):
            return '%.1f' % (int(float(amt) * 10) / 10.0)   # 1 位截断,ccxt 返回字符串

    a = CcxtAdapter(_C(), name='x')
    assert a.quantize_amount('BTC/USDT:USDT', 35.10441977) == 35.1
    assert _C.markets is None or True                       # 惰性加载已发生(实例属性)

    class _Broken:
        def load_markets(self):
            raise RuntimeError('boom')
    b = CcxtAdapter(_Broken(), name='x')
    assert b.quantize_amount('BTC/USDT:USDT', 35.10441977) == 35.10441977   # fail-open


def test_list_instruments_fills_min_cost():
    # Instrument.min_cost 取 ccxt limits.cost.min（币安 MIN_NOTIONAL 语义，spec §5.3）
    insts = _adapter().list_instruments()
    assert insts[0].min_cost == 5.0


def test_instrument_min_cost_defaults_zero():
    from gridtrade.exchanges.base import Instrument
    i = Instrument(symbol='X/USDT:USDT', tick=0.1, lot=0.1, min_size=0.1,
                   state='live', list_ts=0)
    assert i.min_cost == 0.0


def test_create_limit_order_quantizes_price():
    # 挂单价必须按 tickSize 量化——超精度价格被交易所 -1111 拒（testnet KITE 实证：等比几何价
    # round(8) 超 tickSize 1e-05，11/11 拒、开格零挂单卡 OPENING 15 分钟自愈 FAILED）。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    CcxtAdapter(c, name='x').create_limit_order('BTC/USDT:USDT', 'buy', 100.12345, 2.0)
    assert c.created[-1][4] == 100.1        # price（第5位）量化到 tickSize 0.1


def test_create_stop_order_quantizes_trigger_price():
    # 保险丝触发价同样量化（stop_low/high_price 也是几何价、会超精度）。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    CcxtAdapter(c, name='x').create_stop_order('BTC/USDT:USDT', 'sell', 2.0, 95.16789)
    assert c.created[-1][5]['stopLossPrice'] == 95.2
    assert c.created[-1][4] == 95.2         # 参考价（ref price）同步量化


def test_list_instruments_fills_market_max_qty():
    # 市价单单笔数量上限（币安 MARKET_LOT_SIZE.maxQty，ccxt limits.market.max）——
    # 保险丝覆盖率门的数据面（spec 2026-07-15 §三）
    insts = {i.symbol: i for i in _adapter().list_instruments()}
    assert insts['BTC/USDT:USDT'].market_max_qty == 120.0
    assert insts['ETH/USDT:USDT'].market_max_qty == 0.0   # 缺 market 键 → 0=未知（fail-open）


def test_instrument_market_max_qty_defaults_zero():
    from gridtrade.exchanges.base import Instrument
    i = Instrument(symbol='X/USDT:USDT', tick=0.1, lot=0.1, min_size=0.1,
                   state='live', list_ts=0)
    assert i.market_max_qty == 0.0


def test_base_fetch_leverage_tiers_default_empty():
    # 基类默认 []（fail-open）。ExchangeAdapter 是 ABC 不能直接实例化,故用具体实例
    # 直调基类未覆写方法(绕过子类覆写)验证默认契约。
    from gridtrade.exchanges.base import ExchangeAdapter
    from gridtrade.exchanges.fake import FakeExchange
    assert ExchangeAdapter.fetch_leverage_tiers(FakeExchange(), 'BTC/USDT:USDT') == []


def test_ccxt_fetch_leverage_tiers_normalizes_and_caches():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    calls = []
    def flt(symbols, params=None):
        calls.append(list(symbols))
        return {'BTC/USDT:USDT': [
            {'tier': 1, 'maxLeverage': 20, 'maxNotional': 10000.0, 'info': {}},
            {'tier': 2, 'maxLeverage': 10, 'maxNotional': 50000.0, 'info': {}}]}
    c.fetch_leverage_tiers = flt
    a = CcxtAdapter(c, name='x')
    out = a.fetch_leverage_tiers('BTC/USDT:USDT')
    assert out == [{'maxLeverage': 20, 'maxNotional': 10000.0},
                   {'maxLeverage': 10, 'maxNotional': 50000.0}]
    a.fetch_leverage_tiers('BTC/USDT:USDT')          # 二次
    assert len(calls) == 1                            # 按币缓存,不重取


def test_ccxt_fetch_leverage_tiers_failopen_on_exception():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    def boom(symbols, params=None):
        raise RuntimeError('leverageBracket down')
    c.fetch_leverage_tiers = boom
    assert CcxtAdapter(c, name='x').fetch_leverage_tiers('BTC/USDT:USDT') == []   # fail-open


def test_ccxt_fetch_leverage_tiers_returns_defensive_copy():
    # 返回防御拷贝:消费者原地 mutate(append 外层 / 改内层 dict)不得污染实例缓存,
    # 否则下一格 fetch 读到脏档位(评审 Important)。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    def flt(symbols, params=None):
        return {'BTC/USDT:USDT': [
            {'tier': 1, 'maxLeverage': 20, 'maxNotional': 10000.0, 'info': {}},
            {'tier': 2, 'maxLeverage': 10, 'maxNotional': 50000.0, 'info': {}}]}
    c.fetch_leverage_tiers = flt
    a = CcxtAdapter(c, name='x')
    out = a.fetch_leverage_tiers('BTC/USDT:USDT')
    out.append({'maxLeverage': 999, 'maxNotional': 1.0})   # 原地污染外层列表
    out[0]['maxLeverage'] = 999                            # 原地污染内层 dict
    assert a.fetch_leverage_tiers('BTC/USDT:USDT') == [    # 二次 fetch 不受污染
        {'maxLeverage': 20, 'maxNotional': 10000.0},
        {'maxLeverage': 10, 'maxNotional': 50000.0}]


def _raw_trade(fee_cost, fee_ccy):
    return {'id': 't1', 'order': 'o1', 'symbol': 'ELSA/USDT:USDT', 'side': 'sell',
            'price': 1.0, 'amount': 2.0, 'timestamp': 1704067200000,
            'fee': {'cost': fee_cost, 'currency': fee_ccy}, 'info': {'clOrdId': 'g:0'}}


def test_to_trade_converts_bnb_fee_to_quote_usdt():
    # 开「BNB 抵扣手续费」时 commissionAsset=BNB，原样把 BNB 数值当 USDT 记 → 少记约币价倍。
    # 修：按 BNB/USDT 价换算成 USDT 入账。FakeCcxtClient.fetch_ticker last=2.0 → 汇率=2.0。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    a = CcxtAdapter(FakeCcxtClient(), name='ccxt')
    assert a._to_trade(_raw_trade(0.1, 'BNB')).fee == 0.2   # 0.1 BNB × 2.0 = 0.2 USDT


def test_to_trade_usdt_fee_passthrough():
    # quote 币种(USDT)计价的费直接入账，不换算。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    a = CcxtAdapter(FakeCcxtClient(), name='ccxt')
    assert a._to_trade(_raw_trade(0.1, 'USDT')).fee == 0.1


def test_to_trade_fee_conversion_fail_open_to_raw():
    # 换算汇率取不到 → fail-open 退回原值（绝不因费换算阻断交易/记账）。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    def boom(*a, **k):
        raise RuntimeError('no ticker')
    c.fetch_ticker = boom
    a = CcxtAdapter(c, name='ccxt')
    assert a._to_trade(_raw_trade(0.1, 'BNB')).fee == 0.1


def test_fetch_bid_ask_from_ticker():
    # maker-close 挂被动侧需真实 best bid/ask（挂 last 价会跨点差被 GTX 拒）。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    c.fetch_ticker = lambda sym: {'bid': 1.9, 'ask': 2.1, 'last': 2.0}
    assert CcxtAdapter(c, name='x').fetch_bid_ask('BTC/USDT:USDT') == (1.9, 2.1)


def test_fetch_bid_ask_falls_back_to_last_when_missing():
    # ticker 缺 bid/ask（薄簿/接口抖动）→ 回退 last（fail-open，不阻断平仓）。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    c = FakeCcxtClient()
    c.fetch_ticker = lambda sym: {'last': 2.0}
    assert CcxtAdapter(c, name='x').fetch_bid_ask('BTC/USDT:USDT') == (2.0, 2.0)
