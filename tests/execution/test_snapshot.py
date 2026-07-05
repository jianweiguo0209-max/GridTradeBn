# tests/execution/test_snapshot.py
"""AccountSnapshot：视图过滤/构建/失败传播。数据源用 FakeExchange（base 默认 _all）。"""
import pytest

from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.snapshot import AccountSnapshot, build_account_snapshot

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'


def _fake():
    insts = [Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(ETH, 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts, price=100.0)
    ex.set_price(BTC, 100.0); ex.set_price(ETH, 200.0)
    ex.create_limit_order(BTC, 'buy', 99.0, 1.0, client_oid='b1')
    ex.create_limit_order(ETH, 'sell', 201.0, 2.0, client_oid='e1')
    ex.set_price(BTC, 98.5)                     # BTC 买单成交
    return ex


def test_build_and_views():
    ex = _fake()
    snap = build_account_snapshot(ex, [BTC, ETH])
    assert snap.trades_for(BTC) == ex.fetch_my_trades(BTC)
    assert snap.trades_for(ETH) == []
    assert [o.id for o in snap.orders_for(ETH)] == [o.id for o in ex.fetch_open_orders(ETH)]
    assert snap.position(BTC) == ex.fetch_positions(BTC).net_size
    assert snap.price(ETH) == 200.0
    assert snap.price('NOPE/USDT:USDT') is None     # 缺币价 → None（调用方降级）
    assert snap.funding_for(BTC) == ex.fetch_funding_payments(BTC)


def test_trades_for_since_filter():
    ex = _fake()
    snap = build_account_snapshot(ex, [BTC])
    ts = snap.trades_for(BTC)[0].ts
    assert snap.trades_for(BTC, since_ms=ts) != []      # 含边界（>=）
    assert snap.trades_for(BTC, since_ms=ts + 1) == []


def test_build_failure_propagates():
    ex = _fake()
    def boom(symbols, since_ms=None):
        raise RuntimeError('endpoint down')
    ex.fetch_my_trades_all = boom
    with pytest.raises(RuntimeError):
        build_account_snapshot(ex, [BTC])
