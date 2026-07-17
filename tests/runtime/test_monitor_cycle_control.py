import json
from gridtrade.runtime.cycles import run_monitor_cycle
from gridtrade.state.control import CommandRepository, AuditRepository, ControlFlagRepository
from gridtrade.state.models import CMD_DONE


class _Grids:
    def list_active(self): return []
class _Fills:
    def max_ts(self, gid): return 0
class _Accounting:
    def get(self, gid): return None
class _Executor:
    def __init__(self):
        self.grids = _Grids(); self.closed = []
        self.fills = _Fills(); self.accounting = _Accounting()
        from gridtrade.exchanges.fake import FakeExchange
        self.adapter = FakeExchange(instruments=[], price=1.0)   # 快照构建可用（返回空集）
    def is_loaded(self, gid): return True
    def sync(self, gid, symbol, *, skip_replenish=False): pass
    def close(self, gid, symbol, reason): self.closed.append(gid)
class _Manager:
    def __init__(self): self.executor = _Executor()
    def monitor_all(self, skip_replenish=False):
        self.last_skip = skip_replenish; return []
class _Reconciler:
    def __init__(self, ex): self.ex = ex


def test_monitor_cycle_consumes_one_command(store):
    cmds = CommandRepository(store); audit = AuditRepository(store)
    flags = ControlFlagRepository(store)
    cmds.enqueue('CLOSE_GRID', json.dumps({'grid_id': 'g1', 'symbol': 'BTC/USDT:USDT'}),
                 created_by='admin')
    ex = _Executor(); mgr = _Manager(); mgr.executor = ex
    run_monitor_cycle(_Reconciler(ex), mgr, flags=flags, commands=cmds, audit=audit,
                      exchange='hyperliquid')
    assert ex.closed == ['g1']                          # 指令被消费执行
    assert cmds.list_recent()[0].status == CMD_DONE


def test_monitor_cycle_halt_skips_replenish(store, monkeypatch):
    # halt 标志须传到每个网格单元的 monitor_grid(skip_replenish=True)（补单开关的唯一接缝）。
    class _Grid:
        id = 'g1'; symbol = 'BTC/USDT:USDT'; status = 'ACTIVE'
        exchange = 'fake'; tag = ''; created_at = 0
    class _ActiveGrids:
        def list_active(self): return [_Grid()]
    flags = ControlFlagRepository(store); flags.set('trading_halted', True, actor='admin')
    ex = _Executor(); ex.grids = _ActiveGrids()
    mgr = _Manager(); mgr.executor = ex
    mgr.signals = None; mgr.stop_cfg = {}; mgr.margin_rate = 0.05
    seen = {}
    def _fake_monitor_grid(executor, gid, symbol, stop_cfg, *, skip_replenish=False, **kw):
        seen['skip'] = skip_replenish
        return {'closed': False, 'reason': None, 'pnl_ratio': 0.0, 'fills': []}
    monkeypatch.setattr('gridtrade.runtime.cycles.monitor_grid', _fake_monitor_grid)
    class _Rec(_Reconciler):
        def restore(self, gid): pass
        def reconcile_open_orders(self, gid, sym): return {'canceled': 0, 'replaced': 0}
        def check_position_drift(self, gid, sym): return None
        def reconcile_fuses(self, gid, sym): return {}
    run_monitor_cycle(_Rec(ex), mgr, flags=flags,
                      commands=CommandRepository(store), audit=AuditRepository(store),
                      exchange='hyperliquid')
    assert seen['skip'] is True                          # halt → 单元收到 skip_replenish=True
