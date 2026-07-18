from gridtrade.config import load_deploy_config


def _cfg(**kw):
    env = {'EXCHANGE': 'fake'}      # fake -> 无需凭证；database_url 空 -> in_memory
    env.update(kw)
    return load_deploy_config(env=env)


def test_build_runtime_wires_all_components():
    from gridtrade.runtime.factory import build_runtime, Runtime
    from gridtrade.exchanges.resilient_adapter import ResilientAdapter
    from gridtrade.execution.grid_executor import GridExecutor
    from gridtrade.execution.manager import GridManager
    from gridtrade.execution.triggers import TriggerEngine
    from gridtrade.execution.reconciler import Reconciler
    from gridtrade.state.heartbeats import HeartbeatRepository

    rt = build_runtime(_cfg(CAP='500', GRID_GEARING='2.72', MAX_CONCURRENT='7'))
    assert isinstance(rt, Runtime)
    assert isinstance(rt.adapter, ResilientAdapter)
    assert isinstance(rt.executor, GridExecutor)
    assert isinstance(rt.manager, GridManager)
    assert isinstance(rt.trigger_engine, TriggerEngine)
    assert isinstance(rt.reconciler, Reconciler)
    assert isinstance(rt.heartbeats, HeartbeatRepository)
    # config 透传到执行器
    assert rt.executor.cap == 500.0 and rt.executor.gearing == 2.72   # =旧 lev4×0.68


def test_build_runtime_gate_chain_has_five_gates_fuse_before_cap_consumers():
    # FuseCoverageGate 必须在"吃 cap"的门（RiskBudget/MinNotional/Margin）之前：
    # 它写回 proposal.cap，后续门须看到定稿 cap（spec 2026-07-15 §五）。MarginGate 仍末位
    # （短路链中过它即准入，预留不虚高）。SymbolLockGate 已删（同币 cap 裁决收敛到 DB 槽位 +
    # open_proposals 捕获 SlotExhausted）。
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.gates import (FuseCoverageGate, MarginGate,
                                           MaxConcurrentGate, MinNotionalGate,
                                           RiskBudgetGate)
    rt = build_runtime(_cfg())
    gates = rt.manager.gates.gates
    assert len(gates) == 5
    assert isinstance(gates[0], MaxConcurrentGate)
    assert isinstance(gates[1], FuseCoverageGate)
    assert isinstance(gates[2], RiskBudgetGate)
    assert isinstance(gates[3], MinNotionalGate)
    assert isinstance(gates[4], MarginGate)


def test_build_runtime_creates_tables_and_trigger_uses_engine():
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    rt = build_runtime(_cfg())
    # create_all 已建表：list_active 不报错（空库返回 []）
    assert rt.executor.grids.list_active() == []
    # 触发引擎装了 ScheduledSelectionTrigger
    assert any(isinstance(t, ScheduledSelectionTrigger)
               for t in rt.trigger_engine.triggers)


def test_build_runtime_threads_quote_currency_override():
    from gridtrade.runtime.factory import build_runtime
    rt = build_runtime(_cfg(EXCHANGE='binance', BINANCE_API_KEY='k',
                            BINANCE_API_SECRET='s', QUOTE_CURRENCY='USDC'))
    assert rt.adapter._inner.quote_currency == 'USDC'


def test_build_runtime_quote_currency_defaults_to_class_value():
    from gridtrade.runtime.factory import build_runtime
    rt = build_runtime(_cfg(EXCHANGE='binance', BINANCE_API_KEY='k',
                            BINANCE_API_SECRET='s'))
    assert rt.adapter._inner.quote_currency == 'USDT'


def test_build_runtime_manager_shares_executor_and_bus_wired():
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.events import EventBus
    rt = build_runtime(_cfg())
    assert rt.manager.executor is rt.executor
    assert isinstance(rt.event_bus, EventBus)
    assert rt.manager.bus is rt.event_bus


def test_gate_chain_uses_eff_concurrency_and_margin_k():
    # spec 2026-07-18-margin-gate-exchange-im：offset 启用集收紧并发 N 后，
    # MaxConcurrentGate 上限须同步 = eff_concurrency（frac 已按 N 放大 cap，12 兜不住）；
    # MarginGate.k 从 env MARGIN_GATE_K 透传。
    from gridtrade.runtime.factory import build_runtime
    rt = build_runtime(_cfg(LIVE_OPEN_OFFSETS='2,4', MARGIN_GATE_K='1.5'))
    gates = rt.manager.gates.gates
    assert gates[0].max_concurrent == 2
    assert gates[4].k == 1.5


def test_gate_chain_defaults_unchanged_without_offsets():
    from gridtrade.runtime.factory import build_runtime
    rt = build_runtime(_cfg())
    gates = rt.manager.gates.gates
    assert gates[0].max_concurrent == 12       # 零行为变更回归护栏
    assert gates[4].k == 1.25


def test_gate_chain_rejections_persisted_to_store():
    # 拒绝动作可查（2026-07-18 实证：拒因只打 stdout 会随 fly logs 滚掉）：
    # factory 装配 on_reject → gate_rejections 落库,psql/面板可查
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.gates import GateResult, GridProposal
    from gridtrade.state.gate_audit import GateRejectionRepository
    rt = build_runtime(_cfg())
    assert rt.manager.gates.on_reject is not None
    p = GridProposal(exchange='fake', symbol='MET/USDT:USDT', tag='gt2',
                     grid_params={})
    rt.manager.gates.on_reject(p, GateResult(False, 'MarginGate', 'test reason'))
    rows = GateRejectionRepository(rt.store).list_recent(limit=5)
    assert rows and rows[0]['symbol'] == 'MET/USDT:USDT'
    assert rows[0]['gate'] == 'MarginGate' and rows[0]['reason'] == 'test reason'
