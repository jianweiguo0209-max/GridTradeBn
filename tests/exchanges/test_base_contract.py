import pytest


def test_dataclasses_fields():
    from gridtrade.exchanges.base import Instrument, Balance, Position, Order, Trade
    inst = Instrument(symbol='BTC/USDT:USDT', tick=0.1, lot=0.001, min_size=0.001,
                      state='live', list_ts=0)
    assert inst.symbol == 'BTC/USDT:USDT'
    assert Balance(equity=1.0, cash=0.5).cash == 0.5
    assert Position(symbol='BTC/USDT:USDT', net_size=-1.0, avg_price=100.0).net_size == -1.0
    o = Order(id='1', client_oid='g:0', symbol='BTC/USDT:USDT', side='buy',
              price=1.0, size=2.0, filled=0.0, status='open', reduce_only=False)
    assert o.client_oid == 'g:0'
    assert Trade(id='t', client_oid='g:0', symbol='X', side='buy', price=1.0,
                 size=1.0, fee=0.1, ts=0).fee == 0.1


def test_adapter_is_abstract():
    from gridtrade.exchanges.base import ExchangeAdapter
    with pytest.raises(TypeError):
        ExchangeAdapter()  # 抽象类不能实例化


def test_adapter_declares_required_methods():
    from gridtrade.exchanges.base import ExchangeAdapter
    required = {'list_instruments', 'fetch_ohlcv', 'fetch_funding_history',
               'fetch_price', 'fetch_balance', 'fetch_positions',
               'create_limit_order', 'create_market_order', 'cancel_order',
               'cancel_all', 'fetch_open_orders', 'fetch_my_trades',
               'set_leverage', 'exchange_status'}
    abstract = ExchangeAdapter.__abstractmethods__
    assert required.issubset(abstract), f'缺少抽象方法: {required - abstract}'


def test_column_constants():
    from gridtrade.exchanges.base import CANDLE_COLS, FUNDING_COLS
    assert CANDLE_COLS == ['symbol', 'candle_begin_time', 'open', 'high', 'low',
                           'close', 'vol', 'volCcy', 'quote_volume']
    assert FUNDING_COLS == ['ts', 'symbol', 'fundingRate', 'realizedRate']


def test_resilient_adapter_forwards_quantize_amount():
    """ResilientAdapter 逐方法显式转发、无 __getattr__——端口新增方法漏转发会静默落到
    基类恒等默认(2026-07-12 mainnet 实证:量化修复线上失效)。合同:quantize 必达 inner。"""
    from gridtrade.exchanges.resilient_adapter import ResilientAdapter

    class _Inner:
        name = 'x'
        def quantize_amount(self, symbol, amount):
            return 3.7

    r = ResilientAdapter(_Inner())
    assert r.quantize_amount('BTC/USDT:USDT', 3.7827) == 3.7
