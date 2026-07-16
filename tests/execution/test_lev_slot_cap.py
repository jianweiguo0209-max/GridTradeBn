# tests/execution/test_lev_slot_cap.py
"""组件四集成:executor.open 槽位上限杠杆感知——币安重标 lev_caps=((10,1),):maxlev≤10 币同币
第 2 格被 DB 槽位拒(cap 1);>10 走 tier2_cap=2;maxlev=None(FakeExchange 默认)→ tier2_cap=2
(既有测试零波及)。"""
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


def test_lev5_symbol_capped_at_one(store):
    # 币安重标:maxlev≤10(含 5x 脆弱尾部)→ cap 1,同币第 2 格被拒
    gx = _gx(store, 5.0)
    gx.open('fake', BTC, dict(GP), tag='a')
    with pytest.raises(SlotExhausted):
        gx.open('fake', BTC, dict(GP), tag='b')


def test_lev10_symbol_capped_at_one(store):
    # ≤10 上界:maxlev=10 仍 cap 1
    gx = _gx(store, 10.0)
    gx.open('fake', BTC, dict(GP), tag='a')
    with pytest.raises(SlotExhausted):
        gx.open('fake', BTC, dict(GP), tag='b')


def test_lev20_symbol_keeps_tier2_cap(store):
    # >10:maxlev=20 走 tier2_cap=2,第 3 格才被拒
    gx = _gx(store, 20.0)
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
