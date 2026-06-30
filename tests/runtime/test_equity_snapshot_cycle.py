# tests/runtime/test_equity_snapshot_cycle.py
from gridtrade.runtime.cycles import run_monitor_cycle
from gridtrade.state.equity import EquitySnapshotRepository
from gridtrade.exchanges.base import Balance


class _Grids:
    def list_active(self): return []
class _Adapter:
    def __init__(self, raise_=False): self._raise = raise_
    def fetch_balance(self):
        if self._raise: raise RuntimeError('rate limited')
        return Balance(equity=499.0, cash=400.0)
class _Executor:
    def __init__(self, adapter): self.grids = _Grids(); self.adapter = adapter
    def is_loaded(self, gid): return True
class _Manager:
    def __init__(self, adapter): self.executor = _Executor(adapter)
    def monitor_all(self, skip_replenish=False): return []
class _Reconciler:
    def __init__(self, ex): self.ex = ex


def test_cycle_writes_equity_snapshot(store):
    repo = EquitySnapshotRepository(store)
    mgr = _Manager(_Adapter())
    run_monitor_cycle(_Reconciler(mgr.executor), mgr, equity_repo=repo,
                      snapshot_interval_sec=0)
    rows = repo.list_range(0)
    assert len(rows) == 1 and rows[0].equity == 499.0


def test_cycle_survives_balance_error(store):
    repo = EquitySnapshotRepository(store)
    mgr = _Manager(_Adapter(raise_=True))
    # 取余额抛错不应让 cycle 崩
    run_monitor_cycle(_Reconciler(mgr.executor), mgr, equity_repo=repo,
                      snapshot_interval_sec=0)
    assert repo.list_range(0) == []        # 没写，但没崩
