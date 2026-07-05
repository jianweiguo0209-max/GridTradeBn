# tests/exchanges/test_account_batch_base.py
"""base 默认账户级方法 = 逐 symbol 合成（任何交易所天然可用）。差分等价测试。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'


def _fake():
    insts = [Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(ETH, 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts, price=100.0)
    ex.set_price(BTC, 100.0); ex.set_price(ETH, 200.0)
    ex.create_limit_order(BTC, 'buy', 99.0, 1.0, client_oid='b1')
    ex.create_limit_order(ETH, 'sell', 201.0, 2.0, client_oid='e1')
    ex.set_price(BTC, 98.5)          # 触发 BTC 买单成交 → trades/positions 非空
    return ex


def test_trades_all_equals_per_symbol_merged_sorted():
    ex = _fake()
    manual = sorted(ex.fetch_my_trades(BTC) + ex.fetch_my_trades(ETH), key=lambda t: t.ts)
    assert ex.fetch_my_trades_all([BTC, ETH]) == manual
    assert manual                      # 场景确实有成交（防空转真空）


def test_open_orders_all_equals_per_symbol():
    ex = _fake()
    got = {o.id for o in ex.fetch_open_orders_all([BTC, ETH])}
    manual = {o.id for o in ex.fetch_open_orders(BTC) + ex.fetch_open_orders(ETH)}
    assert got == manual and manual


def test_positions_prices_funding_all_equal_per_symbol():
    ex = _fake()
    assert ex.fetch_positions_all([BTC, ETH]) == {
        BTC: ex.fetch_positions(BTC).net_size, ETH: ex.fetch_positions(ETH).net_size}
    assert ex.fetch_prices_all([BTC, ETH]) == {BTC: 98.5, ETH: 200.0}
    assert ex.fetch_funding_payments_all([BTC, ETH]) == {
        BTC: ex.fetch_funding_payments(BTC), ETH: ex.fetch_funding_payments(ETH)}
