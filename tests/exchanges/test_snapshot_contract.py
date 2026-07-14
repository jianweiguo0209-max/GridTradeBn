"""快照六方法契约守卫（spec 2026-07-14 §四）：monitor 唯一读取口。
fake 与 BinanceAdapter(mock) 共用同一套用例——未来 WsFeedAdapter 镜像实现
对着本文件开发，上层零改动。契约：调用时刻最新已知状态、canonical symbol 键、
只读幂等、列表按 ts 升序；不泄漏 REST 假设（分页/权重/时序）。"""
import pytest

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Balance, FundingPayment, Order
from tests.exchanges.test_binance_adapter import FakeBinanceClient, _binance

SYM = 'BTC/USDT:USDT'
OTHER = 'ETH/USDT:USDT'


@pytest.fixture(params=['fake', 'binance'])
def adapter(request):
    if request.param == 'fake':
        ex = FakeExchange()
        ex.set_price(SYM, 100.0)
        ex.set_price(OTHER, 50.0)
        # 两笔市价成交（ts 单调）→ 排序契约非空载；另一币挂单 → "只回请求"过滤非空载
        ex.create_market_order(SYM, 'buy', 1.0, client_oid='1:0:0')
        ex.create_market_order(SYM, 'sell', 0.5, client_oid='1:1:0')
        ex.create_limit_order(SYM, 'buy', 90.0, 1.0, client_oid='1:2:0')
        ex.create_limit_order(OTHER, 'buy', 40.0, 1.0, client_oid='2:0:0')
        ex.seed_funding_payments(SYM, [(2000, 0.5), (1000, -0.3)])
        return ex
    c = FakeBinanceClient()
    # 账户级 openOrders 含未请求币（DOGE）→ 过滤契约非空载（评审：原桩回 symbol=None 行致空载）
    def open_orders(symbol=None, since=None, limit=None, params=None):
        return [
            {'id': '7', 'clientOrderId': '1:0:0', 'symbol': 'BTC/USDT:USDT',
             'side': 'buy', 'price': 1.0, 'amount': 2.0, 'filled': 0.0,
             'status': 'open'},
            {'id': '8', 'clientOrderId': '2:0:0', 'symbol': 'DOGE/USDT:USDT',
             'side': 'buy', 'price': 1.0, 'amount': 2.0, 'filled': 0.0,
             'status': 'open'},
        ]
    c.fetch_open_orders = open_orders
    # 两笔乱序成交 → 基类合成路径的 ts 升序排序被真实检验
    def my_trades(symbol=None, since=None, limit=None, params=None):
        return [
            {'id': 't2', 'order': 'o2', 'symbol': symbol, 'side': 'buy',
             'price': 1.0, 'amount': 2.0, 'timestamp': 2000,
             'fee': {'cost': 0.1}, 'info': {}},
            {'id': 't1', 'order': 'o1', 'symbol': symbol, 'side': 'sell',
             'price': 1.0, 'amount': 1.0, 'timestamp': 1000,
             'fee': {'cost': 0.1}, 'info': {}},
        ]
    c.fetch_my_trades = my_trades
    return _binance(c)


def test_prices_all_float_by_canonical(adapter):
    out = adapter.fetch_prices_all([SYM])
    assert set(out) == {SYM} and isinstance(out[SYM], float)


def test_positions_all_signed_float(adapter):
    out = adapter.fetch_positions_all([SYM])
    for v in out.values():
        assert isinstance(v, float)


def test_open_orders_all_only_requested(adapter):
    out = adapter.fetch_open_orders_all([SYM])
    assert len(out) >= 1                    # 非空载：账本里确有本币挂单
    assert all(isinstance(o, Order) and o.symbol == SYM for o in out)


def test_my_trades_all_sorted(adapter):
    out = adapter.fetch_my_trades_all([SYM])
    assert len(out) >= 2                    # 非空载：≥2 笔才真正检验排序
    ts = [t.ts for t in out]
    assert ts == sorted(ts)


def test_funding_payments_all_sorted(adapter):
    out = adapter.fetch_funding_payments_all([SYM], since_ms=0)
    assert set(out) == {SYM}
    ts = [p.ts for p in out[SYM]]
    assert ts == sorted(ts)
    assert all(isinstance(p, FundingPayment) for p in out[SYM])


def test_balance_shape(adapter):
    b = adapter.fetch_balance()
    assert isinstance(b, Balance)
