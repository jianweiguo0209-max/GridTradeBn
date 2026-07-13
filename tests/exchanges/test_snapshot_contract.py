"""快照六方法契约守卫（spec 2026-07-14 §四）：monitor 唯一读取口。
fake 与 BinanceAdapter(mock) 共用同一套用例——未来 WsFeedAdapter 镜像实现
对着本文件开发，上层零改动。契约：调用时刻最新已知状态、canonical symbol 键、
只读幂等、列表按 ts 升序；不泄漏 REST 假设（分页/权重/时序）。"""
import pytest

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Balance, FundingPayment, Order
from tests.exchanges.test_binance_adapter import FakeBinanceClient, _binance

SYM = 'BTC/USDT:USDT'


def _fake():
    ex = FakeExchange()
    ex.set_price(SYM, 100.0)
    ex.create_limit_order(SYM, 'buy', 90.0, 1.0, client_oid='1:0:0')
    ex.seed_funding_payments(SYM, [(2000, 0.5), (1000, -0.3)])
    return ex


@pytest.fixture(params=['fake', 'binance'])
def adapter(request):
    return _fake() if request.param == 'fake' else _binance(FakeBinanceClient())


def test_prices_all_float_by_canonical(adapter):
    out = adapter.fetch_prices_all([SYM])
    assert set(out) == {SYM} and isinstance(out[SYM], float)


def test_positions_all_signed_float(adapter):
    out = adapter.fetch_positions_all([SYM])
    for v in out.values():
        assert isinstance(v, float)


def test_open_orders_all_only_requested(adapter):
    out = adapter.fetch_open_orders_all([SYM])
    assert all(isinstance(o, Order) and o.symbol == SYM for o in out)


def test_my_trades_all_sorted(adapter):
    out = adapter.fetch_my_trades_all([SYM])
    assert [t.ts for t in out] == sorted(t.ts for t in out)


def test_funding_payments_all_sorted(adapter):
    out = adapter.fetch_funding_payments_all([SYM], since_ms=0)
    assert set(out) == {SYM}
    ts = [p.ts for p in out[SYM]]
    assert ts == sorted(ts)
    assert all(isinstance(p, FundingPayment) for p in out[SYM])


def test_balance_shape(adapter):
    b = adapter.fetch_balance()
    assert isinstance(b, Balance)
