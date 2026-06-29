import pytest

from gridtrade.execution.gates import (GridProposal, GateResult, AdmissionGate,
                                       GateChain)


def _proposal(symbol='BTC/USDT:USDT', **kw):
    base = dict(exchange='okx', symbol=symbol,
                grid_params={'low_price': 90.0, 'high_price': 110.0,
                             'grid_count': 10, 'stop_low_price': 85.0,
                             'stop_high_price': 115.0})
    base.update(kw)
    return GridProposal(**base)


class _AlwaysPass(AdmissionGate):
    def check(self, proposal):
        return GateResult(True, 'AlwaysPass')


class _AlwaysFail(AdmissionGate):
    def __init__(self, reason='nope'):
        self.reason = reason
    def check(self, proposal):
        return GateResult(False, 'AlwaysFail', self.reason)


def test_chain_all_pass_returns_passed():
    chain = GateChain([_AlwaysPass(), _AlwaysPass()])
    r = chain.evaluate(_proposal())
    assert r.passed is True


def test_chain_short_circuits_on_first_failure():
    # 第二门失败；第三门即便会失败也不应被求值（用 raise 哨兵证明短路）
    class _Boom(AdmissionGate):
        def check(self, proposal):
            raise AssertionError('should not be evaluated after a failure')
    chain = GateChain([_AlwaysPass(), _AlwaysFail('blocked'), _Boom()])
    r = chain.evaluate(_proposal())
    assert r.passed is False
    assert r.gate == 'AlwaysFail' and r.reason == 'blocked'


def test_chain_filter_keeps_only_passing_proposals():
    # 偶数索引放行、奇数拦截，验证 filter 批量语义
    class _BySymbol(AdmissionGate):
        def check(self, proposal):
            ok = proposal.symbol != 'ETH/USDT:USDT'
            return GateResult(ok, 'BySymbol', '' if ok else 'eth blocked')
    chain = GateChain([_BySymbol()])
    props = [_proposal('BTC/USDT:USDT'), _proposal('ETH/USDT:USDT'),
             _proposal('SOL/USDT:USDT')]
    kept = chain.filter(props)
    assert [p.symbol for p in kept] == ['BTC/USDT:USDT', 'SOL/USDT:USDT']


def test_empty_chain_passes():
    assert GateChain([]).evaluate(_proposal()).passed is True


def _grid_repo_with(*active_symbols, exchange='okx'):
    from gridtrade.state.store import StateStore
    from gridtrade.state.grids import GridRepository
    from gridtrade.state.models import Grid, ACTIVE
    s = StateStore.in_memory(); s.create_all()
    repo = GridRepository(s)
    for sym in active_symbols:
        repo.create(Grid(id='', exchange=exchange, symbol=sym, status=ACTIVE))
    return repo


def test_symbol_lock_blocks_when_active_grid_exists():
    from gridtrade.execution.gates import SymbolLockGate
    repo = _grid_repo_with('BTC/USDT:USDT')
    gate = SymbolLockGate(repo)
    r = gate.check(_proposal('BTC/USDT:USDT'))
    assert r.passed is False and r.gate == 'SymbolLockGate'


def test_symbol_lock_allows_free_symbol():
    from gridtrade.execution.gates import SymbolLockGate
    repo = _grid_repo_with('BTC/USDT:USDT')
    gate = SymbolLockGate(repo)
    assert gate.check(_proposal('ETH/USDT:USDT')).passed is True


def test_symbol_lock_is_per_exchange():
    from gridtrade.execution.gates import SymbolLockGate
    repo = _grid_repo_with('BTC/USDT:USDT', exchange='okx')
    gate = SymbolLockGate(repo)
    # 同币种但不同交易所 -> 放行
    assert gate.check(_proposal('BTC/USDT:USDT', exchange='hyperliquid')).passed is True


def test_max_concurrent_blocks_at_limit():
    from gridtrade.execution.gates import MaxConcurrentGate
    repo = _grid_repo_with('BTC/USDT:USDT', 'ETH/USDT:USDT')
    gate = MaxConcurrentGate(repo, max_concurrent=2)
    r = gate.check(_proposal('SOL/USDT:USDT'))
    assert r.passed is False and r.gate == 'MaxConcurrentGate'


def test_max_concurrent_allows_below_limit():
    from gridtrade.execution.gates import MaxConcurrentGate
    repo = _grid_repo_with('BTC/USDT:USDT')
    gate = MaxConcurrentGate(repo, max_concurrent=2)
    assert gate.check(_proposal('SOL/USDT:USDT')).passed is True


def test_max_concurrent_zero_active_allows():
    from gridtrade.execution.gates import MaxConcurrentGate
    repo = _grid_repo_with()
    gate = MaxConcurrentGate(repo, max_concurrent=1)
    assert gate.check(_proposal('BTC/USDT:USDT')).passed is True


def _grid_repo_with_caps(*caps, exchange='okx'):
    from gridtrade.state.store import StateStore
    from gridtrade.state.grids import GridRepository
    from gridtrade.state.models import Grid, ACTIVE
    s = StateStore.in_memory(); s.create_all()
    repo = GridRepository(s)
    for i, cap in enumerate(caps):
        repo.create(Grid(id='', exchange=exchange, symbol='S%d/USDT:USDT' % i,
                         status=ACTIVE, cap=cap))
    return repo


def test_risk_budget_blocks_when_cap_sum_exceeds():
    from gridtrade.execution.gates import RiskBudgetGate
    repo = _grid_repo_with_caps(60.0, 30.0)  # used = 90
    gate = RiskBudgetGate(repo, total_budget=100.0, default_cap=50.0)
    # 显式 cap=20 -> 90 + 20 = 110 > 100 -> 拒绝
    r = gate.check(_proposal(cap=20.0))
    assert r.passed is False and r.gate == 'RiskBudgetGate'


def test_risk_budget_allows_within_budget():
    from gridtrade.execution.gates import RiskBudgetGate
    repo = _grid_repo_with_caps(60.0)
    gate = RiskBudgetGate(repo, total_budget=100.0, default_cap=50.0)
    assert gate.check(_proposal(cap=20.0)).passed is True  # 60 + 20 = 80 <= 100


def test_risk_budget_uses_default_cap_when_proposal_cap_none():
    from gridtrade.execution.gates import RiskBudgetGate
    repo = _grid_repo_with_caps(60.0)
    gate = RiskBudgetGate(repo, total_budget=100.0, default_cap=50.0)
    # proposal.cap None -> 用 default 50 -> 60 + 50 = 110 > 100 -> 拒绝
    assert gate.check(_proposal()).passed is False


def test_risk_budget_at_exact_limit_allows():
    from gridtrade.execution.gates import RiskBudgetGate
    repo = _grid_repo_with_caps(80.0)
    gate = RiskBudgetGate(repo, total_budget=100.0, default_cap=50.0)
    # 80 + 20 = 100 == budget -> 放行（<=）
    assert gate.check(_proposal(cap=20.0)).passed is True


class _BalAdapter:
    """最小余额桩：可配可用余额；raises=True 模拟 fetch_balance 抛错。"""
    def __init__(self, cash, raises=False):
        self._cash = cash; self._raises = raises; self.calls = 0
    def fetch_balance(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError('balance down')
        from gridtrade.exchanges.base import Balance
        return Balance(equity=self._cash, cash=self._cash)


def test_margin_gate_allows_when_cash_ge_cap():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(100.0), default_cap=100.0)
    assert gate.check(_proposal()).passed is True       # 惰性快照, 100>=100


def test_margin_gate_blocks_when_cash_below_required():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(50.0), default_cap=100.0)
    r = gate.check(_proposal())
    assert r.passed is False and r.gate == 'MarginGate'


def test_margin_gate_uses_explicit_proposal_cap():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(30.0), default_cap=100.0)
    assert gate.check(_proposal(cap=20.0)).passed is True   # 用显式 20 而非 default 100


def test_margin_gate_cumulative_deduction():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(250.0), default_cap=100.0)
    gate.begin_batch()
    assert gate.check(_proposal()).passed is True       # reserve 100, 余 150
    assert gate.check(_proposal()).passed is True       # reserve 200, 余 50
    r = gate.check(_proposal())                          # 需 100 > 50 -> 拒
    assert r.passed is False and r.gate == 'MarginGate'


def test_margin_gate_begin_batch_resets_reservation():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(100.0), default_cap=100.0)
    gate.begin_batch()
    assert gate.check(_proposal()).passed is True        # reserve 100
    assert gate.check(_proposal()).passed is False       # 余 0 < 100
    gate.begin_batch()                                   # 新批: 预留清零 + 重拉余额
    assert gate.check(_proposal()).passed is True        # 又 100>=100


def test_margin_gate_fail_closed_on_balance_error():
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(1000.0, raises=True), default_cap=100.0)
    gate.begin_batch()
    r = gate.check(_proposal())
    assert r.passed is False and r.reason == 'balance unavailable'


def test_margin_gate_in_chain_filter_cumulative_and_per_batch_snapshot():
    from gridtrade.execution.gates import MarginGate
    adapter = _BalAdapter(250.0)
    chain = GateChain([MarginGate(adapter, default_cap=100.0)])
    props = [_proposal('BTC/USDT:USDT'), _proposal('ETH/USDT:USDT'),
             _proposal('SOL/USDT:USDT')]
    kept = chain.filter(props)
    assert [p.symbol for p in kept] == ['BTC/USDT:USDT', 'ETH/USDT:USDT']  # 第三超额被拒
    kept2 = chain.filter(props)                           # 新批 -> begin_batch 重置
    assert len(kept2) == 2
    assert adapter.calls == 2                             # 每批仅快照一次余额
