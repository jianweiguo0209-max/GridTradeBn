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

    rt = build_runtime(_cfg(CAP='500', LEVERAGE='4', MAX_CONCURRENT='7'))
    assert isinstance(rt, Runtime)
    assert isinstance(rt.adapter, ResilientAdapter)
    assert isinstance(rt.executor, GridExecutor)
    assert isinstance(rt.manager, GridManager)
    assert isinstance(rt.trigger_engine, TriggerEngine)
    assert isinstance(rt.reconciler, Reconciler)
    assert isinstance(rt.heartbeats, HeartbeatRepository)
    # config 透传到执行器
    assert rt.executor.cap == 500.0 and rt.executor.leverage == 4.0


def test_build_runtime_gate_chain_has_three_gates():
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.gates import (SymbolLockGate, MaxConcurrentGate,
                                          RiskBudgetGate)
    rt = build_runtime(_cfg())
    gates = rt.manager.gates.gates
    assert len(gates) == 3
    assert isinstance(gates[0], SymbolLockGate)
    assert isinstance(gates[1], MaxConcurrentGate)
    assert isinstance(gates[2], RiskBudgetGate)


def test_build_runtime_creates_tables_and_trigger_uses_engine():
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.triggers import ScheduledSelectionTrigger
    rt = build_runtime(_cfg())
    # create_all 已建表：list_active 不报错（空库返回 []）
    assert rt.executor.grids.list_active() == []
    # 触发引擎装了 ScheduledSelectionTrigger
    assert any(isinstance(t, ScheduledSelectionTrigger)
               for t in rt.trigger_engine.triggers)


def test_build_runtime_manager_shares_executor_and_bus_wired():
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.events import EventBus
    rt = build_runtime(_cfg())
    assert rt.manager.executor is rt.executor
    assert isinstance(rt.event_bus, EventBus)
    assert rt.manager.bus is rt.event_bus
