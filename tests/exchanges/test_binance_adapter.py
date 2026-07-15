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
                              # market = MARKET_LOT_SIZE（市价单单笔上限；ccxt 标准映射）
                              'limits': {'amount': {'min': 0.001}, 'cost': {'min': 50.0},
                                         'market': {'min': 0.001, 'max': 120.0}},
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
    def fetch_positions(self, symbols=None, params=None):
        # 无参=全账户 positionRisk（币安权重5）
        return [{'symbol': 'BTC/USDT:USDT', 'contracts': 3.0, 'side': 'long',
                 'entryPrice': 100.0},
                {'symbol': 'ETH/USDT:USDT', 'contracts': 2.0, 'side': 'short',
                 'entryPrice': 50.0}]
    def fapiPublicGetTickerPrice(self, params=None):
        return [{'symbol': 'BTCUSDT', 'price': '50000.5'},
                {'symbol': 'ETHUSDT', 'price': '3000.25'},
                {'symbol': 'BTCUSDC', 'price': '49999.0'}]
    def fapiPrivateGetIncome(self, params=None):
        self.income_calls = getattr(self, 'income_calls', [])
        self.income_calls.append(dict(params or {}))
        return [
            {'symbol': 'BTCUSDT', 'incomeType': 'FUNDING_FEE', 'income': '-0.5',
             'time': 2000},
            {'symbol': 'ETHUSDT', 'incomeType': 'FUNDING_FEE', 'income': '0.3',
             'time': 1000},
            {'symbol': 'XRPUSDT', 'incomeType': 'FUNDING_FEE', 'income': '9.9',
             'time': 1500},
        ]
    def set_margin_mode(self, mode, symbol=None, params=None):
        self.margin_calls = getattr(self, 'margin_calls', [])
        self.margin_calls.append((mode, symbol))
    def fapiPrivateGetPositionSideDual(self, params=None):
        return {'dualSidePosition': getattr(self, 'dual', False)}
    def fapiPrivateGetMultiAssetsMargin(self, params=None):
        return {'multiAssetsMargin': getattr(self, 'multi', False)}


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


def test_from_credentials_testnet_demo_trading():
    import ccxt
    from gridtrade.exchanges.binance import BinanceAdapter
    a = BinanceAdapter.from_credentials('k', 's', testnet=True)
    assert isinstance(a.client, ccxt.binanceusdm)
    # 币安期货 testnet 已弃用：testnet=True 走 Demo Trading，API 指向 demo-fapi.binance.com
    assert 'demo' in str(a.client.urls['api']).lower()


def test_from_credentials_allows_accountwide_open_orders():
    # ccxt binanceusdm 默认对无 symbol 的 fetchOpenOrders 抛错护栏；账户级快照需要全账户
    # 一次(权重40)，必须显式关闭，否则 monitor 快照上线即死（终审 Critical 1）。
    from gridtrade.exchanges.binance import BinanceAdapter
    a = BinanceAdapter.from_credentials('k', 's')
    assert a.client.options['warnOnFetchOpenOrdersWithoutSymbol'] is False


def test_market_id_fallback_uses_symbol_own_quote():
    # 未知/极新上市 symbol：回退拼接须用符号自身 quote，绝不可静默映射到适配器的
    # quote_currency（旧 HL USDC 符号不可能变成 USDT 行情）——终审 Minor 修复。
    a = _binance()
    assert a._market_id('FOO/USDC:USDC') == 'FOOUSDC'


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


def test_fetch_my_trades_clamps_ancient_since():
    # 币安 7 天窗语义:since=0(=1970) 必须收敛到近 6.5 天,近期 since 原样,None 透传
    c = FakeBinanceClient()
    seen = {}
    def my_trades(symbol=None, since=None, limit=None, params=None):
        seen['since'] = since
        return []
    c.fetch_my_trades = my_trades
    c.milliseconds = lambda: 10_000_000_000_000
    a = _binance(c)
    a.fetch_my_trades('BTC/USDT:USDT', since_ms=0)
    assert seen['since'] == 10_000_000_000_000 - int(6.5 * 24 * 3600 * 1000)
    a.fetch_my_trades('BTC/USDT:USDT', since_ms=10_000_000_000_000 - 1000)
    assert seen['since'] == 10_000_000_000_000 - 1000
    a.fetch_my_trades('BTC/USDT:USDT')
    assert seen['since'] is None


def test_fetch_open_orders_all_single_call():
    c = FakeBinanceClient()
    calls = []
    def fetch_open_orders(symbol=None, since=None, limit=None, params=None):
        assert since is None, '错位: params 传进了 since'
        calls.append(symbol)
        return [{'id': '7', 'clientOrderId': '1:0:0', 'symbol': 'BTC/USDT:USDT',
                 'side': 'buy', 'price': 1.0, 'amount': 2.0, 'filled': 0.0,
                 'status': 'open'},
                {'id': '8', 'clientOrderId': '2:0:0', 'symbol': 'DOGE/USDT:USDT',
                 'side': 'buy', 'price': 1.0, 'amount': 2.0, 'filled': 0.0,
                 'status': 'open'}]
    c.fetch_open_orders = fetch_open_orders
    out = _binance(c).fetch_open_orders_all(['BTC/USDT:USDT'])
    # 无 symbol=全账户，两簿各一次（常规 + algo 触发单；demo 实测 2026-07-14）
    assert calls == [None, None]
    assert all(o.symbol == 'BTC/USDT:USDT' for o in out)   # 只回请求的 symbols


def test_fetch_positions_all_signed():
    out = _binance().fetch_positions_all(['BTC/USDT:USDT', 'ETH/USDT:USDT',
                                          'SOL/USDT:USDT'])
    assert out['BTC/USDT:USDT'] == 3.0
    assert out['ETH/USDT:USDT'] == -2.0          # short → 负
    assert 'SOL/USDT:USDT' not in out            # 无持仓行=缺省（调用方按0处理）


def test_fetch_prices_all_ticker_price():
    out = _binance().fetch_prices_all(['BTC/USDT:USDT', 'ETH/USDT:USDT'])
    assert out == {'BTC/USDT:USDT': 50000.5, 'ETH/USDT:USDT': 3000.25}


def test_fetch_funding_payments_all_income_grouped():
    c = FakeBinanceClient()
    out = _binance(c).fetch_funding_payments_all(
        ['BTC/USDT:USDT', 'ETH/USDT:USDT'], since_ms=500)
    # income 正=收入 → 统一"支付为正"取负；按币分组、ts 升序；XRP 不在请求内被丢弃
    assert [(p.ts, p.amount) for p in out['BTC/USDT:USDT']] == [(2000, 0.5)]
    assert [(p.ts, p.amount) for p in out['ETH/USDT:USDT']] == [(1000, -0.3)]
    assert c.income_calls[0]['incomeType'] == 'FUNDING_FEE'
    assert c.income_calls[0]['startTime'] == 500


def test_fetch_funding_payments_all_pagination_tie_no_loss():
    # 页界(1000)切在 8 行同刻并列组中间：含边界重取+去重后零丢失（评审实证场景）
    c = FakeBinanceClient()
    rows = []
    for i in range(997):
        rows.append({'symbol': 'BTCUSDT', 'incomeType': 'FUNDING_FEE',
                     'income': '0.001', 'time': 1000 + i, 'tranId': i})
    for j in range(8):
        rows.append({'symbol': ('BTCUSDT' if j % 2 else 'ETHUSDT'),
                     'incomeType': 'FUNDING_FEE', 'income': '0.001',
                     'time': 5000, 'tranId': 2000 + j})
    def income(params=None):
        p = dict(params or {})
        start = int(p.get('startTime', 0))
        eligible = [r for r in rows if int(r['time']) >= start]
        return eligible[:int(p.get('limit', 1000))]
    c.fapiPrivateGetIncome = income
    out = _binance(c).fetch_funding_payments_all(
        ['BTC/USDT:USDT', 'ETH/USDT:USDT'], since_ms=0)
    total = len(out['BTC/USDT:USDT']) + len(out['ETH/USDT:USDT'])
    assert total == 1005


def test_create_stop_order_stop_market():
    c = FakeBinanceClient()
    a = _binance(c)
    a.create_stop_order('BTC/USDT:USDT', 'sell', 1.5, 95.0, client_oid='9:fuse:low')
    sym, typ, side, amount, price, params = c.created[-1]
    assert typ == 'market' and price is None            # STOP_MARKET：无限价
    assert params['stopLossPrice'] == 95.0
    assert params['reduceOnly'] is True
    assert params['clientOrderId'] == '9:fuse:low'
    assert 'slippage' not in params                     # 币安无滑点底线参数（spec §5.2）


def test_create_stop_order_clamps_to_market_max_qty():
    # 保险丝数量按 MARKET_LOT_SIZE.maxQty 封顶（testnet PORTAL 实证 2026-07-14：
    # worst=order_num×grid_count 超市价单上限 → -4005 拒单、开格卡 OPENING）。
    # reduce-only 触发时按实际持仓执行，封顶后语义不变；超限部分软止损+爆仓线兜底。
    c = FakeBinanceClient()
    a = _binance(c)
    a.create_stop_order('BTC/USDT:USDT', 'sell', 500.0, 95.0, client_oid='9:fuse:low')
    _, typ, _, amount, _, params = c.created[-1]
    assert typ == 'market' and amount == 120.0          # 500 超上限 → 封顶到 maxQty
    assert params['reduceOnly'] is True                 # 封顶不改 reduce-only 语义
    a.create_stop_order('BTC/USDT:USDT', 'sell', 1.5, 95.0)
    assert c.created[-1][3] == 1.5                      # 未超限原样
    a.create_stop_order('ETH/USDT:USDT', 'buy', 9999.0, 105.0)
    assert c.created[-1][3] == 9999.0                   # 无 market.max（缺失）→ fail-open 不封顶


def test_create_stop_order_quantizes_trigger_price():
    # 触发价按 tickSize 量化（stop_low/high_price 也是等比几何价、会超精度 → -1111 拒）
    c = FakeBinanceClient()
    _binance(c).create_stop_order('BTC/USDT:USDT', 'sell', 1.5, 95.16789, client_oid='9:fuse:low')
    assert c.created[-1][5]['stopLossPrice'] == 95.2   # tickSize 0.1


def test_set_leverage_cross_then_int():
    c = FakeBinanceClient()
    lev_calls = []
    c.set_leverage = lambda lev, symbol=None, params=None: lev_calls.append((lev, symbol))
    _binance(c).set_leverage('BTC/USDT:USDT', 5.0)
    assert c.margin_calls == [('cross', 'BTC/USDT:USDT')]
    assert lev_calls == [(5, 'BTC/USDT:USDT')]          # 币安要求整数杠杆


def test_set_leverage_swallows_no_need_to_change():
    c = FakeBinanceClient()
    def boom(mode, symbol=None, params=None):
        raise RuntimeError('binanceusdm {"code":-4046,"msg":"No need to change margin type."}')
    c.set_margin_mode = boom
    lev_calls = []
    c.set_leverage = lambda lev, symbol=None, params=None: lev_calls.append(lev)
    _binance(c).set_leverage('BTC/USDT:USDT', 5)        # 不抛
    assert lev_calls == [5]


def test_set_leverage_reraises_unrelated_margin_errors():
    # 变异守卫（评审实证）：非 -4046 异常必须重抛且不得走到设杠杆——防"吞一切"回归
    import pytest
    c = FakeBinanceClient()
    def boom(mode, symbol=None, params=None):
        raise RuntimeError('binanceusdm {"code":-2015,"msg":"Invalid API-key"}')
    c.set_margin_mode = boom
    lev_calls = []
    c.set_leverage = lambda lev, symbol=None, params=None: lev_calls.append(lev)
    with pytest.raises(RuntimeError):
        _binance(c).set_leverage('BTC/USDT:USDT', 5)
    assert lev_calls == []


def test_assert_account_mode_ok_and_rejects():
    import pytest
    c = FakeBinanceClient()
    a = _binance(c)
    a.assert_account_mode()                             # 单向+单币 → 通过
    c.dual = True
    with pytest.raises(RuntimeError):
        a.assert_account_mode()
    c.dual = False; c.multi = 'true'                    # 字符串布尔也要识别
    with pytest.raises(RuntimeError):
        a.assert_account_mode()


def test_base_and_resilient_assert_account_mode():
    from gridtrade.exchanges.fake import FakeExchange
    from gridtrade.exchanges.resilient_adapter import ResilientAdapter
    FakeExchange().assert_account_mode()                # 基类默认 no-op
    called = []
    class Probe(FakeExchange):
        def assert_account_mode(self):
            called.append(1)
    ResilientAdapter(Probe()).assert_account_mode()     # 直通转发
    assert called == [1]


def test_cancel_order_falls_back_to_trigger_book():
    # demo 实测(2026-07-14)：STOP_MARKET 在独立 algo 簿，常规撤单 -2011 → trigger 回退
    import ccxt
    c = FakeBinanceClient()
    calls = []
    def cancel(order_id, symbol=None, params=None):
        calls.append((order_id, dict(params or {})))
        if not (params or {}).get('trigger'):
            raise ccxt.OrderNotFound('binanceusdm {"code":-2011,"msg":"Unknown order sent."}')
    c.cancel_order = cancel
    _binance(c).cancel_order('BTC/USDT:USDT', '1000000135187438')
    assert calls[0][1] == {} and calls[1][1] == {'trigger': True}


def test_cancel_order_regular_success_no_fallback():
    c = FakeBinanceClient()
    calls = []
    c.cancel_order = lambda oid, symbol=None, params=None: calls.append(dict(params or {}))
    _binance(c).cancel_order('BTC/USDT:USDT', '123')
    assert calls == [{}]                       # 常规成功不再多打 algo 簿


def test_fetch_open_orders_merges_trigger_book():
    # 保险丝可见性：单币挂单=常规簿+algo 簿并读（不并读→对账器误判丝丢失反复重挂）
    c = FakeBinanceClient()
    def open_orders(symbol=None, since=None, limit=None, params=None):
        assert since is None, '错位: params 传进了 since'
        if (params or {}).get('trigger'):
            return [{'id': '9', 'clientOrderId': '1:fuse:low', 'symbol': 'BTC/USDT:USDT',
                     'side': 'sell', 'price': 95.0, 'amount': 1.0, 'filled': 0.0,
                     'status': 'open', 'info': {'reduceOnly': True}}]
        return [{'id': '7', 'clientOrderId': '1:0:0', 'symbol': 'BTC/USDT:USDT',
                 'side': 'buy', 'price': 1.0, 'amount': 2.0, 'filled': 0.0,
                 'status': 'open'}]
    c.fetch_open_orders = open_orders
    a = _binance(c)
    assert sorted(o.id for o in a.fetch_open_orders('BTC/USDT:USDT')) == ['7', '9']
    assert sorted(o.id for o in a.fetch_open_orders_all(['BTC/USDT:USDT'])) == ['7', '9']


def test_cancel_all_clears_both_books():
    # 关格两簿齐清：常规 allOpenOrders + algo algoOpenOrders（防残留丝关格后触发）
    c = FakeBinanceClient()
    calls = []
    c.cancel_all_orders = lambda symbol=None, params=None: calls.append(dict(params or {}))
    _binance(c).cancel_all('BTC/USDT:USDT')
    assert calls == [{}, {'trigger': True}]


def test_encode_cloid_compresses_uuid_gid():
    # testnet 实证（2026-07-14）：grid_id 为 32-hex uuid，'{gid}:fuse:low' 全长 41 字符
    # 越界致保险丝下单失败、格卡 OPENING——gid 段压缩到前 12 hex 后全格式 ≤22 字符
    a = _binance()
    gid = '1fc96ed264af48319fba276ea8d240b0'
    assert a.encode_cloid('%s:0:0' % gid) == '%s:0:0' % gid[:12]
    assert a.encode_cloid('%s:fuse:low' % gid) == '%s:fuse:low' % gid[:12]
    assert a.encode_cloid('%s:close:3' % gid) == '%s:close:3' % gid[:12]
    assert len(a.encode_cloid('%s:fuse:high' % gid)) == 22
    # 短 gid（冒烟脚本 '999999:1:1'）不受压缩影响
    assert a.encode_cloid('999999:1:1') == '999999:1:1'
    # 确定性：同输入恒同输出（交易所端幂等去重语义保持）
    assert a.encode_cloid('%s:5:2' % gid) == a.encode_cloid('%s:5:2' % gid)
