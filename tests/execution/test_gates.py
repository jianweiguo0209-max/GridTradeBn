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


def test_chain_filter_logs_each_rejection():
    # 可观测性：被门拒掉的提案必须留痕（symbol + gate + reason），
    # 否则「该开未开」无法事后定位（cf. 2026-06-30 11:00 JUP 静默被丢）。
    logs = []

    class _BySymbol(AdmissionGate):
        def check(self, proposal):
            ok = proposal.symbol != 'ETH/USDT:USDT'
            return GateResult(ok, 'BySymbol', '' if ok else 'eth blocked')

    chain = GateChain([_BySymbol()], log=logs.append)
    chain.filter([_proposal('BTC/USDT:USDT'), _proposal('ETH/USDT:USDT')])
    assert any('ETH/USDT:USDT' in m and 'BySymbol' in m and 'eth blocked' in m
               for m in logs), logs
    # 放行的提案不应被当作拒绝打日志
    assert not any('BTC/USDT:USDT' in m for m in logs), logs


def test_chain_filter_without_log_is_silent_and_works():
    # 向后兼容：不传 log 不报错、过滤语义不变
    class _BySymbol(AdmissionGate):
        def check(self, proposal):
            ok = proposal.symbol != 'ETH/USDT:USDT'
            return GateResult(ok, 'BySymbol', '' if ok else 'blocked')

    kept = GateChain([_BySymbol()]).filter(
        [_proposal('BTC/USDT:USDT'), _proposal('ETH/USDT:USDT')])
    assert [p.symbol for p in kept] == ['BTC/USDT:USDT']


def _grid_repo_with(store, *active_symbols, exchange='okx'):
    from gridtrade.state.grids import GridRepository
    from gridtrade.state.models import Grid, ACTIVE
    repo = GridRepository(store)
    for sym in active_symbols:
        repo.create(Grid(id='', exchange=exchange, symbol=sym, status=ACTIVE))
    return repo


def test_max_concurrent_blocks_at_limit(store):
    from gridtrade.execution.gates import MaxConcurrentGate
    repo = _grid_repo_with(store, 'BTC/USDT:USDT', 'ETH/USDT:USDT')
    gate = MaxConcurrentGate(repo, max_concurrent=2)
    r = gate.check(_proposal('SOL/USDT:USDT'))
    assert r.passed is False and r.gate == 'MaxConcurrentGate'


def test_max_concurrent_allows_below_limit(store):
    from gridtrade.execution.gates import MaxConcurrentGate
    repo = _grid_repo_with(store, 'BTC/USDT:USDT')
    gate = MaxConcurrentGate(repo, max_concurrent=2)
    assert gate.check(_proposal('SOL/USDT:USDT')).passed is True


def test_max_concurrent_zero_active_allows(store):
    from gridtrade.execution.gates import MaxConcurrentGate
    repo = _grid_repo_with(store)
    gate = MaxConcurrentGate(repo, max_concurrent=1)
    assert gate.check(_proposal('BTC/USDT:USDT')).passed is True


def _grid_repo_with_caps(store, *caps, exchange='okx'):
    from gridtrade.state.grids import GridRepository
    from gridtrade.state.models import Grid, ACTIVE
    repo = GridRepository(store)
    for i, cap in enumerate(caps):
        repo.create(Grid(id='', exchange=exchange, symbol='S%d/USDT:USDT' % i,
                         status=ACTIVE, cap=cap))
    return repo


def test_risk_budget_blocks_when_cap_sum_exceeds(store):
    from gridtrade.execution.gates import RiskBudgetGate
    repo = _grid_repo_with_caps(store, 60.0, 30.0)  # used = 90
    gate = RiskBudgetGate(repo, total_budget=100.0, default_cap=50.0)
    # 显式 cap=20 -> 90 + 20 = 110 > 100 -> 拒绝
    r = gate.check(_proposal(cap=20.0))
    assert r.passed is False and r.gate == 'RiskBudgetGate'


def test_risk_budget_allows_within_budget(store):
    from gridtrade.execution.gates import RiskBudgetGate
    repo = _grid_repo_with_caps(store, 60.0)
    gate = RiskBudgetGate(repo, total_budget=100.0, default_cap=50.0)
    assert gate.check(_proposal(cap=20.0)).passed is True  # 60 + 20 = 80 <= 100


def test_risk_budget_uses_default_cap_when_proposal_cap_none(store):
    from gridtrade.execution.gates import RiskBudgetGate
    repo = _grid_repo_with_caps(store, 60.0)
    gate = RiskBudgetGate(repo, total_budget=100.0, default_cap=50.0)
    # proposal.cap None -> 用 default 50 -> 60 + 50 = 110 > 100 -> 拒绝
    assert gate.check(_proposal()).passed is False


def test_risk_budget_at_exact_limit_allows(store):
    from gridtrade.execution.gates import RiskBudgetGate
    repo = _grid_repo_with_caps(store, 80.0)
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


def test_margin_gate_logs_swallowed_balance_exception():
    # fail-closed 不再静默：被吞掉的真实余额异常必须打出来，否则整批被拒却零线索
    # （cf. MarginGate 静默 fail-closed → 整轮开仓被无声丢弃）。
    from gridtrade.execution.gates import MarginGate
    logs = []
    gate = MarginGate(_BalAdapter(1000.0, raises=True), default_cap=100.0,
                      log=logs.append)
    gate.begin_batch()
    assert any('MarginGate' in m and ('balance down' in m or 'RuntimeError' in m)
               for m in logs), logs


def test_margin_gate_log_silent_on_success():
    # 正常取到余额时不应打错误日志
    from gridtrade.execution.gates import MarginGate
    logs = []
    gate = MarginGate(_BalAdapter(1000.0), default_cap=100.0, log=logs.append)
    gate.begin_batch()
    assert logs == []


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


class _SizerStub:
    """MinNotionalGate 只依赖 executor 的 sizing 表面：_resolve_cap()/gearing/min_amount。"""
    def __init__(self, cap=102.0, gearing=2.5, min_amount=0.0):   # 2.5=旧 lev5×max_rate0.5
        self._cap = cap
        self.gearing = gearing
        self.min_amount = min_amount

    def _resolve_cap(self):
        return self._cap


def test_min_notional_gate_blocks_dense_grid():
    # 密网币：cap=102/lev=5/max_rate=0.5、41 档 low=1.0 → 单笔最低档名义额≈$5 < $10 → 拒
    from gridtrade.execution.gates import MinNotionalGate
    gate = MinNotionalGate(_SizerStub(), min_notional=10.0)
    p = _proposal(grid_params={'low_price': 1.0, 'high_price': 1.5, 'grid_count': 41,
                               'stop_low_price': 0.9, 'stop_high_price': 1.6})
    r = gate.check(p)
    assert r.passed is False and r.gate == 'MinNotionalGate'
    assert '10' in (r.reason or '')                       # 拒因含最小额，可观测


def test_min_notional_gate_allows_sparse_grid():
    # 疏网：同 cap、10 档 → 单笔≈$18.9 ≥ $10 → 放行
    from gridtrade.execution.gates import MinNotionalGate
    gate = MinNotionalGate(_SizerStub(), min_notional=10.0)
    p = _proposal(grid_params={'low_price': 1.0, 'high_price': 1.5, 'grid_count': 10,
                               'stop_low_price': 0.9, 'stop_high_price': 1.6})
    assert gate.check(p).passed is True


def test_min_notional_gate_disabled_when_zero():
    # min_notional<=0 = 停用（默认，向后兼容：无此约束的交易所不受影响）
    from gridtrade.execution.gates import MinNotionalGate
    gate = MinNotionalGate(_SizerStub(), min_notional=0.0)
    p = _proposal(grid_params={'low_price': 1.0, 'high_price': 1.5, 'grid_count': 149,
                               'stop_low_price': 0.9, 'stop_high_price': 1.6})
    assert gate.check(p).passed is True


def test_margin_gate_dynamic_reserve_uses_executor_resolve_cap():
    # 方案B：提案未带 cap 时，预留额 = executor._resolve_cap()（与真实开仓同源动态 cap）。
    # 差分 load-bearing：cash=250 < 动态 cap 302 → 拒；旧逻辑(default_cap=100)会误放。
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(250.0), default_cap=100.0, executor=_SizerStub(cap=302.0))
    r = gate.check(_proposal())
    assert r.passed is False and r.gate == 'MarginGate'
    assert '302' in (r.reason or '')                    # 拒因含真实预留额，可观测


def test_margin_gate_without_executor_falls_back_to_default_cap():
    # 向后兼容护栏：executor 未传 → 预留额仍为 default_cap（现有调用/测试零改动语义）。
    from gridtrade.execution.gates import MarginGate
    gate = MarginGate(_BalAdapter(250.0), default_cap=100.0)
    assert gate.check(_proposal()).passed is True       # 250 >= 100


def test_min_notional_gate_per_symbol_floor():
    # env 全局下限 0，但该币 Instrument.min_cost=50（如 BTCUSDT）→ 仍按 50 拒
    from gridtrade.execution.gates import MinNotionalGate, GridProposal
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange

    class _Ex:   # 最小 executor 桩：与该文件既有用例同形
        gearing = 3.4
        min_amount = 0.0
        def _resolve_cap(self):
            return 100.0

    fake = FakeExchange(instruments=[
        Instrument(symbol='BTC/USDT:USDT', tick=0.1, lot=0.001, min_size=0.001,
                   state='live', list_ts=0, min_cost=50.0)])
    gate = MinNotionalGate(_Ex(), 0.0, adapter=fake)
    gate.begin_batch()
    gp = dict(low_price=100.0, high_price=120.0, grid_count=20,
              stop_low_price=95.0, stop_high_price=125.0)
    res = gate.check(GridProposal(exchange='binance', symbol='BTC/USDT:USDT',
                                  grid_params=gp))
    assert not res.passed and 'min 50' in res.reason


def test_min_notional_gate_env_floor_still_applies():
    # 币无 min_cost（映射缺省）→ 退回全局 env 下限。
    # 下限取 1000（远高于 cap100×gearing3.4 摊到 21 档的最坏名义额 ≤16.2），必拒——
    # 不依赖 grid_order_info 精确数学，测试稳健。
    from gridtrade.execution.gates import MinNotionalGate, GridProposal
    from gridtrade.exchanges.fake import FakeExchange

    class _Ex:
        gearing = 3.4
        min_amount = 0.0
        def _resolve_cap(self):
            return 100.0

    gate = MinNotionalGate(_Ex(), 1000.0, adapter=FakeExchange())
    gate.begin_batch()
    gp = dict(low_price=100.0, high_price=120.0, grid_count=20,
              stop_low_price=95.0, stop_high_price=125.0)
    res = gate.check(GridProposal(exchange='binance', symbol='X/USDT:USDT',
                                  grid_params=gp))
    assert not res.passed and 'min 1000' in res.reason
    # 微小下限 → 放行（同一提案两个下限对照，锁住 max(env, min_cost) 的方向性）
    gate2 = MinNotionalGate(_Ex(), 0.001, adapter=FakeExchange())
    gate2.begin_batch()
    assert gate2.check(GridProposal(exchange='binance', symbol='X/USDT:USDT',
                                    grid_params=gp)).passed


def test_min_notional_gate_disabled_when_no_floor():
    from gridtrade.execution.gates import MinNotionalGate, GridProposal
    class _Ex:
        gearing = 3.4
        min_amount = 0.0
        def _resolve_cap(self):
            return 100.0
    gate = MinNotionalGate(_Ex(), 0.0)           # 无 adapter、env=0 → 停用
    gp = dict(low_price=100.0, high_price=120.0, grid_count=20,
              stop_low_price=95.0, stop_high_price=125.0)
    assert gate.check(GridProposal(exchange='binance', symbol='X/USDT:USDT',
                                   grid_params=gp)).passed


def _fuse_ex(cap=100.0, cap_min=20.0):
    class _Ex:
        gearing = 3.4
        min_amount = 0.0
        def __init__(self):
            self.cap_min = cap_min
        def _resolve_cap(self):
            return cap
    return _Ex()


def _fuse_gp():
    return dict(low_price=100.0, high_price=120.0, grid_count=20,
                stop_low_price=95.0, stop_high_price=125.0)


def _fuse_adapter(max_qty):
    # FakeExchange 只需提供带 market_max_qty 的 Instrument
    from gridtrade.exchanges.base import Instrument
    from gridtrade.exchanges.fake import FakeExchange
    return FakeExchange(instruments=[
        Instrument(symbol='BTC/USDT:USDT', tick=0.1, lot=0.001, min_size=0.001,
                   state='live', list_ts=0, min_cost=0.0, market_max_qty=max_qty)])


def test_fuse_gate_caps_down_and_writes_back_proposal_cap():
    # 不足额（maxQty=worst/2）→ 降 cap 到刚好足额，写回 proposal.cap 供后续门/executor 用
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    w = fuse_worst(100.0, 3.4, gp)
    gate = FuseCoverageGate(_fuse_ex(), 1.0, adapter=_fuse_adapter(w / 2.0))
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    res = gate.check(p)
    assert res.passed
    assert p.cap == pytest.approx(50.0)                       # 定稿 cap 写回提议
    assert fuse_worst(p.cap, 3.4, gp) <= (w / 2.0) * (1 + 1e-9)   # 丝护全额


def test_fuse_gate_passes_when_covered():
    # 足额 → 放行且不动 cap（proposal.cap 保持 None，executor 用动态 cap）
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    gate = FuseCoverageGate(_fuse_ex(), 1.0,
                            adapter=_fuse_adapter(fuse_worst(100.0, 3.4, gp) * 10))
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    assert gate.check(p).passed and p.cap is None


def test_fuse_gate_rejects_when_capped_below_cap_min():
    # 降档后 cap' < CAP_MIN → 拒（安全失败，不建死网格）
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    w = fuse_worst(100.0, 3.4, gp)
    gate = FuseCoverageGate(_fuse_ex(cap_min=60.0), 1.0,
                            adapter=_fuse_adapter(w * 0.5))     # cap'=50 < CAP_MIN 60
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    res = gate.check(p)
    assert not res.passed and 'CAP_MIN' in res.reason


def test_fuse_gate_fails_open_on_unknown_max_qty_and_adapter_error():
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    # ① maxQty=0（未知）→ 放行不干预
    gate = FuseCoverageGate(_fuse_ex(), 1.0, adapter=_fuse_adapter(0.0))
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    assert gate.check(p).passed and p.cap is None
    # ② list_instruments 抛异常 → 空映射 fail-open（绝不因限额表读不到而拒单）
    class _Boom:
        def list_instruments(self):
            raise RuntimeError('limits unavailable')
    logs = []
    gate2 = FuseCoverageGate(_fuse_ex(), 1.0, adapter=_Boom(), log=logs.append)
    gate2.begin_batch()
    p2 = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    assert gate2.check(p2).passed and p2.cap is None
    assert any('FuseCoverageGate' in m for m in logs)


def test_fuse_gate_disabled_when_min_coverage_zero():
    # 停用开关（紧急回退）：不足额也放行不动 cap
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import FuseCoverageGate, GridProposal
    gp = _fuse_gp()
    gate = FuseCoverageGate(_fuse_ex(), 0.0,
                            adapter=_fuse_adapter(fuse_worst(100.0, 3.4, gp) * 0.1))
    gate.begin_batch()
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    assert gate.check(p).passed and p.cap is None


def test_manager_forwards_capped_cap_to_executor():
    # 集成守卫（评审实测 2026-07-15）：门链降档的 cap 必须真的传到 executor.open——
    # 不传则真实网格按原始 cap 建仓、保险丝照旧超限，本功能等于没做。
    from gridtrade.execution.gates import GateChain, GridProposal
    from gridtrade.execution.manager import GridManager
    seen = {}
    class _Exec:
        def open(self, exchange, symbol, grid_params, *, offset=0, tag='', cap=None):
            seen['cap'] = cap
            return 'gid1'
    mgr = GridManager(_Exec(), GateChain([]), stop_cfg={})   # open_proposals 不读 stop_cfg
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT',
                     grid_params=_fuse_gp(), cap=42.0)     # 模拟 FuseCoverageGate 降档结果
    assert mgr.open_proposals([p]) == ['gid1']
    assert seen['cap'] == 42.0                             # 定稿 cap 到达执行器


def test_manager_isolates_build_failure_per_proposal():
    # 建网失败逐提议隔离（用户定 2026-07-15）：一个密网币不阻断整轮换仓
    from gridtrade.execution.gates import GateChain, GridProposal
    from gridtrade.execution.manager import GridManager
    class _Exec:
        def open(self, exchange, symbol, grid_params, *, offset=0, tag='', cap=None):
            if symbol == 'BAD/USDT:USDT':
                raise RuntimeError('建网失败：保证金不足')
            return 'gid-ok'
    mgr = GridManager(_Exec(), GateChain([]), stop_cfg={})   # open_proposals 不读 stop_cfg
    bad = GridProposal(exchange='binance', symbol='BAD/USDT:USDT', grid_params=_fuse_gp())
    ok = GridProposal(exchange='binance', symbol='OK/USDT:USDT', grid_params=_fuse_gp())
    assert mgr.open_proposals([bad, ok]) == ['gid-ok']     # 坏币跳过、好币照开


def test_fuse_gate_then_min_notional_gate_rejects_unviable_capdown():
    # DRY 分工验证：FuseCoverage 只降 cap，"降后每笔名义额不够"由 MinNotionalGate 自然拒
    from gridtrade.execution.fuse_policy import fuse_worst
    from gridtrade.execution.gates import (FuseCoverageGate, GateChain,
                                           MinNotionalGate, GridProposal)
    gp = _fuse_gp()
    w = fuse_worst(100.0, 3.4, gp)
    ex = _fuse_ex(cap_min=1.0)                      # CAP_MIN 极低 → 不在 FuseGate 被拒
    adapter = _fuse_adapter(w * 0.02)               # 覆盖率 2% → cap'≈2
    chain = GateChain([FuseCoverageGate(ex, 1.0, adapter=adapter),
                       MinNotionalGate(ex, 5.0, adapter=adapter)])
    p = GridProposal(exchange='binance', symbol='BTC/USDT:USDT', grid_params=gp)
    kept = chain.filter([p])
    assert kept == []                                # 被 MinNotionalGate 拒（非 FuseGate）
    assert chain.evaluate(p).gate == 'MinNotionalGate'

