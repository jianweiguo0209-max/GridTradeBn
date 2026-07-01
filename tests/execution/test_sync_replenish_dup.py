"""补对侧单不得在 opp_line 已有 open 单时产生重复挂单。

testnet OP/gt00 实证：中性网格价格震荡，sync 补单只按 opp_line 无条件 create_limit_order，
不检查该 (line,side) 是否已有 resting open 单 → 持久重复挂单（两张都成交则该 line 双倍建仓）。
"""
from collections import Counter

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    return ex, GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def _open_dups(gx, gid):
    opens = [o for o in gx.orders.list_by_grid(gid) if o.status == 'open']
    return {k: v for k, v in Counter((o.line_index, o.side) for o in opens).items() if v > 1}


def test_replenish_no_duplicate_when_opp_line_already_open(store):
    ex, gx = _setup(store, 100.0)
    gid = gx.open('fake', SYM, GP)
    ex.set_price(SYM, 101.6)          # 拉升：上方卖单成交 → 向下补买单，撞上已 resting 的买单
    gx.sync(gid, SYM)
    assert not _open_dups(gx, gid), f"duplicate open orders: {_open_dups(gx, gid)}"


def test_replenish_no_duplicate_across_oscillation(store):
    ex, gx = _setup(store, 100.0)
    gid = gx.open('fake', SYM, GP)
    for p in (101.6, 98.4, 101.6, 98.4):   # 反复震荡，最易叠加重复
        ex.set_price(SYM, p)
        gx.sync(gid, SYM)
        assert not _open_dups(gx, gid), f"duplicate at price={p}: {_open_dups(gx, gid)}"
