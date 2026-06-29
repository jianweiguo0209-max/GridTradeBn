import json
from gridtrade.runtime.cycles import run_monitor_cycle
from gridtrade.state.control import CommandRepository, AuditRepository, ControlFlagRepository
from gridtrade.state.models import CMD_DONE


class _Grids:
    def list_active(self): return []
class _Executor:
    def __init__(self): self.grids = _Grids(); self.closed = []
    def is_loaded(self, gid): return True
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


def test_monitor_cycle_halt_skips_replenish(store):
    flags = ControlFlagRepository(store); flags.set('trading_halted', True, actor='admin')
    ex = _Executor(); mgr = _Manager(); mgr.executor = ex
    run_monitor_cycle(_Reconciler(ex), mgr, flags=flags,
                      commands=CommandRepository(store), audit=AuditRepository(store),
                      exchange='hyperliquid')
    assert mgr.last_skip is True                         # halt → monitor_all(skip_replenish=True)
