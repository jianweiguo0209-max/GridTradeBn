"""build_runtime：从 DeployConfig 一站组装全部运行时组件，返回 Runtime bundle。

守护进程（scheduler/monitor）直接吃 bundle。exchange='fake' + 空 database_url 时
全离线可测。
"""
from dataclasses import dataclass

from gridtrade.config import DEFAULT_STOP_CFG, DEFAULT_STRATEGY_CONFIG
from gridtrade.exchanges.registry import build_adapter
from gridtrade.exchanges.resilience import CircuitBreaker
from gridtrade.exchanges.resilient_adapter import ResilientAdapter
from gridtrade.execution.events import EventBus
from gridtrade.execution.gates import (GateChain, MarginGate, MaxConcurrentGate,
                                       RiskBudgetGate, SymbolLockGate)
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.manager import GridManager
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.triggers import (ScheduledSelectionTrigger,
                                          TriggerEngine)
from gridtrade.state.heartbeats import HeartbeatRepository
from gridtrade.state.store import StateStore


@dataclass
class Runtime:
    config: object
    adapter: object
    store: object
    executor: object
    manager: object
    trigger_engine: object
    reconciler: object
    heartbeats: object
    event_bus: object


def build_runtime(config) -> Runtime:
    inner = build_adapter({
        'exchange': config.exchange,
        'wallet_address': config.wallet_address,
        'private_key': config.private_key,
        'testnet': config.testnet,
    })
    adapter = ResilientAdapter(inner, breaker=CircuitBreaker())

    store = (StateStore.from_url(config.database_url) if config.database_url
             else StateStore.in_memory())
    store.create_all()

    executor = GridExecutor(adapter, store, cap=config.cap,
                            leverage=config.leverage)
    gates = GateChain([
        SymbolLockGate(executor.grids),
        MaxConcurrentGate(executor.grids, config.max_concurrent),
        RiskBudgetGate(executor.grids, config.total_budget, config.default_cap),
        MarginGate(adapter, config.default_cap),
    ])
    bus = EventBus()
    manager = GridManager(executor, gates, stop_cfg=DEFAULT_STOP_CFG,
                          event_bus=bus)

    sc = DEFAULT_STRATEGY_CONFIG
    trigger = ScheduledSelectionTrigger(sc, sc['factors'], sc['weight_list'],
                                        utc_offset=config.utc_offset)
    trigger_engine = TriggerEngine([trigger])

    return Runtime(
        config=config, adapter=adapter, store=store, executor=executor,
        manager=manager, trigger_engine=trigger_engine,
        reconciler=Reconciler(executor),
        heartbeats=HeartbeatRepository(store), event_bus=bus,
    )
