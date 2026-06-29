from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.state.store import StateStore


SYM1 = 'BTC/USDT:USDT'
SYM2 = 'ETH/USDT:USDT'
GP = {'low_price': 90.0, 'high_price': 110.0, 'grid_count': 10,
      'stop_low_price': 80.0, 'stop_high_price': 120.0}


def _executor():
    store = StateStore.in_memory()
    store.create_all()
    ex = FakeExchange(instruments=[
        Instrument(SYM1, 0.1, 0.001, 0.001, 'live', 0),
        Instrument(SYM2, 0.1, 0.001, 0.001, 'live', 0)
    ], price=100.0)
    ex.set_price(SYM1, 100.0)
    ex.set_price(SYM2, 100.0)
    executor = GridExecutor(ex, store, cap=100.0, leverage=5.0)
    return executor, ex


def test_open_uses_cap_override():
    ex, fake_ex = _executor()
    gid = ex.open('fake', SYM1, GP, tag='gt0', cap=250.0)
    grid = ex.grids.get(gid)
    assert grid.cap == 250.0                  # 覆盖值写入网格

    gid2 = ex.open('fake', SYM2, GP, tag='gt0')
    assert ex.grids.get(gid2).cap == 100.0    # 不传 cap → 用 self.cap（行为不变）
