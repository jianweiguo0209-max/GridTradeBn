# tests/execution/test_lev_slot_cap.py
"""组件四集成:executor.open 槽位上限杠杆感知——lev3 币同币第 2 格被 DB 槽位拒;
FakeExchange 默认 max_leverage=None → 原行为(全部既有测试零波及)。"""
import pytest

from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.grids import SlotExhausted

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _gx(store, maxlev):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    if maxlev is not None:
        ex.max_leverage = lambda s: maxlev
    return GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def test_lev3_symbol_capped_at_one(store):
    gx = _gx(store, 3.0)
    gx.open('fake', BTC, dict(GP), tag='a')
    with pytest.raises(SlotExhausted):
        gx.open('fake', BTC, dict(GP), tag='b')


def test_lev5_symbol_capped_at_two(store):
    gx = _gx(store, 5.0)
    gx.open('fake', BTC, dict(GP), tag='a')
    gx.open('fake', BTC, dict(GP), tag='b')
    with pytest.raises(SlotExhausted):
        gx.open('fake', BTC, dict(GP), tag='c')


def test_unknown_lev_keeps_tier2_cap(store):
    gx = _gx(store, None)                     # FakeExchange 默认 → None → tier2_cap=2
    for t in 'ab':
        gx.open('fake', BTC, dict(GP), tag=t)
    with pytest.raises(SlotExhausted):
        gx.open('fake', BTC, dict(GP), tag='c')
