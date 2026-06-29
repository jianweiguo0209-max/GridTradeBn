import ccxt
import pytest

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.faulty import FaultyAdapter, Partial, RaiseAfter

SYM = 'BTC/USDT:USDT'


def _fake():
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=100.0)
    ex.set_price(SYM, 100.0)
    return ex


def test_passthrough_when_no_schedule():
    f = FaultyAdapter(_fake())
    assert f.fetch_price(SYM) == 100.0
    assert f.name == 'fake'                      # 非可调用属性透传


def test_raises_scripted_exception_then_passes_through():
    f = FaultyAdapter(_fake(), {'fetch_price': [ccxt.RequestTimeout('t'), None]})
    with pytest.raises(ccxt.RequestTimeout):
        f.fetch_price(SYM)                       # 第1次：抛
    assert f.fetch_price(SYM) == 100.0           # 第2次：脚本耗尽 → 透传


def test_exception_fault_does_not_touch_inner():
    ex = _fake()
    f = FaultyAdapter(ex, {'create_limit_order': [ccxt.RequestTimeout('t')]})
    with pytest.raises(ccxt.RequestTimeout):
        f.create_limit_order(SYM, 'buy', 99.0, 0.01, client_oid='a:0:0')
    assert ex.fetch_open_orders(SYM) == []       # 请求未达内层 → 无挂单


def test_raise_after_calls_inner_then_raises():
    ex = _fake()
    f = FaultyAdapter(ex, {'create_limit_order': [RaiseAfter(ccxt.RequestTimeout('lost-ack'))]})
    with pytest.raises(ccxt.RequestTimeout):
        f.create_limit_order(SYM, 'buy', 99.0, 0.01, client_oid='a:0:0')
    assert len(ex.fetch_open_orders(SYM)) == 1   # 内层已建单（ack 丢失场景）


def test_partial_reduces_market_size_at_inner():
    ex = _fake()
    f = FaultyAdapter(ex, {'create_market_order': [Partial(0.5)]})
    o = f.create_market_order(SYM, 'buy', 1.0, client_oid='a:init:0')
    assert o.filled == pytest.approx(0.5)
    assert ex.fetch_positions(SYM).net_size == pytest.approx(0.5)  # 内层持仓只动一半
