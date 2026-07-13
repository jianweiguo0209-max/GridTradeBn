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


def test_build_runtime_gate_chain_has_four_gates_margin_last():
    # MarginGate 必须末位（短路链中过它即准入，预留不虚高）；MinNotionalGate 在其前。
    # SymbolLockGate 已删（同币 cap 裁决收敛到 DB 槽位 + open_proposals 捕获 SlotExhausted）。
    from gridtrade.runtime.factory import build_runtime
    from gridtrade.execution.gates import (MaxConcurrentGate, MinNotionalGate,
                                           RiskBudgetGate, MarginGate)
    rt = build_runtime(_cfg())
    gates = rt.manager.gates.gates
    assert len(gates) == 4
    assert isinstance(gates[0], MaxConcurrentGate)
    assert isinstance(gates[1], RiskBudgetGate)
    assert isinstance(gates[2], MinNotionalGate)
    assert isinstance(gates[3], MarginGate)


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
