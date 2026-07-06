# tests/runtime/test_cycles_snapshot.py
"""cycle 快照接线：单元零逐格读、失败整轮跳过、E2 宽限×快照时序不幻影重挂。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.gates import GridProposal, GateChain
from gridtrade.execution.manager import GridManager
from gridtrade.runtime.cycles import run_monitor_cycle

BTC = 'BTC/USDT:USDT'
ETH = 'ETH/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}

PER_SYMBOL_READS = ('fetch_my_trades', 'fetch_open_orders', 'fetch_positions',
                    'fetch_price', 'fetch_funding_payments')


class _Counting:
    """委托 FakeExchange；计数逐 symbol 读调用。_all 走 inner 的 base 默认实现
    （inner 内部自调不经本包装，故计数只反映单元直发的逐格读——应为 0）。"""
    def __init__(self, inner):
        self._inner = inner
        self.reads = {m: 0 for m in PER_SYMBOL_READS}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def fetch_my_trades(self, symbol, since_ms=None):
        self.reads['fetch_my_trades'] += 1
        return self._inner.fetch_my_trades(symbol, since_ms=since_ms)

    def fetch_open_orders(self, symbol):
        self.reads['fetch_open_orders'] += 1
        return self._inner.fetch_open_orders(symbol)

    def fetch_positions(self, symbol):
        self.reads['fetch_positions'] += 1
        return self._inner.fetch_positions(symbol)

    def fetch_price(self, symbol):
        self.reads['fetch_price'] += 1
        return self._inner.fetch_price(symbol)

    def fetch_funding_payments(self, symbol, since_ms=None):
        self.reads['fetch_funding_payments'] += 1
        return self._inner.fetch_funding_payments(symbol, since_ms=since_ms)


def _setup(store):
    insts = [Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0),
             Instrument(ETH, 0.1, 0.001, 0.001, 'live', 0)]
    ex = FakeExchange(instruments=insts, price=100.0)
    ex.set_price(BTC, 100.0); ex.set_price(ETH, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    mgr = GridManager(gx, GateChain([]), stop_cfg=STOP_CFG)
    mgr.open_proposals([
        GridProposal(exchange='fake', symbol=BTC, grid_params=dict(GP), offset=0, tag='t0', source='t'),
        GridProposal(exchange='fake', symbol=ETH, grid_params=dict(GP), offset=0, tag='t1', source='t')])
    return ex, gx, mgr


def test_units_do_zero_per_symbol_reads(store):
    ex, gx, mgr = _setup(store)
    wrapped = _Counting(ex)
    gx.adapter = wrapped
    out = run_monitor_cycle(Reconciler(gx), mgr, parallel=4)
    assert len(out['monitored']) == 2 and out['degraded'] == {}
    assert wrapped.reads == {m: 0 for m in PER_SYMBOL_READS}   # 全部读来自快照


def test_snapshot_failure_skips_round_gracefully(store):
    ex, gx, mgr = _setup(store)

    class _Broken(_Counting):
        def fetch_my_trades_all(self, symbols, since_ms=None):
            raise RuntimeError('batch endpoint down')

    gx.adapter = _Broken(ex)
    logs, beats = [], []
    out = run_monitor_cycle(Reconciler(gx), mgr, parallel=4, log=logs.append,
                            beat=lambda: beats.append(1), beat_every_sec=0.0)
    assert out['monitored'] == [] and out['reconciled'] == {}   # 整轮跳过
    assert any('snapshot failed' in s for s in logs)
    assert beats                                                # 心跳照打


def test_fill_replenish_not_phantom_replaced_across_rounds(store):
    # E2 宽限 × 快照时序：本轮补挂的新单在轮首快照缺席（missing 计 1），
    # 下轮新快照可见（清零）→ 两轮 replaced 均为 0、不产生重复挂单。
    # 反例警示：replace_grace=1 时本语义会被破坏（新单次轮即被误重挂）。
    ex, gx, mgr = _setup(store)
    rec = Reconciler(gx)
    gid = [g.id for g in gx.grids.list_active() if g.symbol == BTC][0]
    ex.set_price(BTC, 100.6)                     # 卖单成交 → sync 补对侧
    out1 = run_monitor_cycle(rec, mgr)
    assert out1['reconciled'][gid]['replaced'] == 0
    out2 = run_monitor_cycle(rec, mgr)
    assert out2['reconciled'][gid]['replaced'] == 0
    lines = [(o.line_index, o.side) for o in gx.orders.list_by_grid(gid)
             if o.status == 'open']
    assert len(lines) == len(set(lines))         # 无重复挂单
