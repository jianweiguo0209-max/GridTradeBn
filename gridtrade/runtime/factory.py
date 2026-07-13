"""build_runtime：从 DeployConfig 一站组装全部运行时组件，返回 Runtime bundle。

守护进程（scheduler/monitor）直接吃 bundle。exchange='fake' + 空 database_url 时
全离线可测。
"""
from dataclasses import dataclass

from gridtrade.config import DEFAULT_STOP_CFG, DEFAULT_STRATEGY_CONFIG
from gridtrade.exchanges.registry import build_adapter
from gridtrade.exchanges.resilient_adapter import ResilientAdapter, default_breakers
from gridtrade.execution.events import EventBus
from gridtrade.execution.gates import (GateChain, MarginGate, MaxConcurrentGate,
                                       MinNotionalGate, RiskBudgetGate)
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.manager import GridManager
from gridtrade.execution.signals import LiveSignalProvider
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.triggers import (ScheduledSelectionTrigger,
                                          TriggerEngine)
from gridtrade.state.equity import EquitySnapshotRepository
from gridtrade.state.heartbeats import HeartbeatRepository
from gridtrade.state.store import StateStore


def _flush_log(msg):   # fly logs 行缓冲：守护进程里 stdout 需 flush 才即时可见
    print(msg, flush=True)


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
    flags: object = None
    commands: object = None
    audit: object = None
    equity: object = None


def build_runtime(config) -> Runtime:
    inner = build_adapter({
        'exchange': config.exchange,
        'api_key': config.api_key,
        'secret': config.api_secret,
        'testnet': config.testnet,
        'quote_currency': config.quote_currency,
    })
    adapter = ResilientAdapter(inner, breakers=default_breakers())

    store = (StateStore.from_url(config.database_url) if config.database_url
             else StateStore.in_memory())
    store.create_all()

    executor = GridExecutor(adapter, store, cap=config.cap,
                            gearing=config.grid_gearing,
                            stop_orders_enabled=config.stop_orders_enabled,
                            stop_slippage=config.stop_slippage,
                            cap_equity_frac=config.cap_equity_frac,
                            cap_min=config.cap_min, cap_max=config.cap_max)
    gates = GateChain([
        MaxConcurrentGate(executor.grids, config.max_concurrent),
        RiskBudgetGate(executor.grids, config.total_budget, config.default_cap),
        MinNotionalGate(executor, config.min_order_notional, adapter=adapter,
                        log=_flush_log),
        MarginGate(adapter, config.default_cap, executor=executor, log=_flush_log),
    ], log=_flush_log)
    bus = EventBus()
    # 实盘退出信号：pv_spike（对齐回测 calc_pv_spike）+ funding_rate（HL 真实费率），按 grid 节流
    signals = LiveSignalProvider(adapter, mult=DEFAULT_STOP_CFG['pv_mult'],
                                 period=DEFAULT_STOP_CFG['pv_period'], n=DEFAULT_STOP_CFG['pv_n'],
                                 log=_flush_log)
    manager = GridManager(executor, gates, stop_cfg=DEFAULT_STOP_CFG,
                          event_bus=bus, signal_provider=signals)

    sc = DEFAULT_STRATEGY_CONFIG
    trigger = ScheduledSelectionTrigger(sc, sc['factors'], sc['weight_list'])
    trigger_engine = TriggerEngine([trigger])

    from gridtrade.state.control import (ControlFlagRepository, CommandRepository,
                                        AuditRepository)
    return Runtime(
        config=config, adapter=adapter, store=store, executor=executor,
        manager=manager, trigger_engine=trigger_engine,
        reconciler=Reconciler(executor),
        heartbeats=HeartbeatRepository(store), event_bus=bus,
        flags=ControlFlagRepository(store), commands=CommandRepository(store),
        audit=AuditRepository(store),
        equity=EquitySnapshotRepository(store),
    )
