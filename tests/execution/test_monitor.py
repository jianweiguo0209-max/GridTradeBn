from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.state.store import StateStore
from gridtrade.state.models import CLOSED

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def _setup(price=100.0):
    ex = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    ex.set_price(SYM, price)
    store = StateStore.in_memory(); store.create_all()
    from gridtrade.execution.grid_executor import GridExecutor
    return ex, store, GridExecutor(ex, store, cap=1000.0, leverage=5.0)


def test_monitor_no_exit_when_flat_pnl():
    from gridtrade.execution.monitor import monitor_grid
    ex, store, gx = _setup(100.0)
    gid = gx.open('fake', SYM, GP)
    out = monitor_grid(gx, gid, SYM, STOP_CFG)
    assert out['closed'] is False and out['reason'] is None


def test_monitor_triggers_fixed_stop_and_closes():
    from gridtrade.execution.monitor import monitor_grid
    from gridtrade.state.grids import GridRepository
    ex, store, gx = _setup(100.0)
    gid = gx.open('fake', SYM, GP)
    # 价格大跌：中性底仓多头浮亏，pnl_ratio 跌破 -3.4% → 固定止损
    ex.set_price(SYM, 96.5)
    out = monitor_grid(gx, gid, SYM, STOP_CFG)
    assert out['closed'] is True and out['reason'] == '固定止损'
    assert GridRepository(store).get(gid).status == CLOSED
    assert abs(ex.fetch_positions(SYM).net_size) < 1e-9   # 已平
