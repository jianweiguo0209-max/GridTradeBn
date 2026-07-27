"""build_runtime：从 DeployConfig 一站组装全部运行时组件，返回 Runtime bundle。

守护进程（scheduler/monitor）直接吃 bundle。exchange='fake' + 空 database_url 时
全离线可测。
"""
from dataclasses import dataclass

from gridtrade.config import DEFAULT_STOP_CFG, DEFAULT_STRATEGY_CONFIG
from gridtrade.exchanges.registry import build_adapter
from gridtrade.exchanges.resilient_adapter import ResilientAdapter, default_breakers
from gridtrade.execution.events import EventBus
from gridtrade.execution.gates import (FuseCoverageGate, GateChain, MarginGate,
                                       MaxConcurrentGate, MinNotionalGate,
                                       RiskBudgetGate)
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


def _gate_reject_audit(store):
    """门拒绝落库钩子(gate_rejections 表,psql/面板可查)：stdout 拒因随 fly logs 分钟级
    滚掉(2026-07-18 mainnet MET 实证),持久化留痕。写失败由 GateChain fail-soft 兜住。"""
    from gridtrade.state.gate_audit import GateRejectionRepository
    repo = GateRejectionRepository(store)

    def _hook(proposal, result):
        repo.add(exchange=proposal.exchange, symbol=proposal.symbol,
                 tag=proposal.tag, gate=result.gate, reason=result.reason)
    return _hook


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
    notifier: object = None
    label_feed: object = None


def build_runtime(config) -> Runtime:
    inner = build_adapter({
        'exchange': config.exchange,
        'api_key': config.api_key,
        'secret': config.api_secret,
        'testnet': config.testnet,
        'quote_currency': config.quote_currency,
        'income_ttl_sec': getattr(config, 'snapshot_income_ttl_sec', 300.0),
        'algo_book_ttl_sec': getattr(config, 'snapshot_algo_book_ttl_sec', 60.0),
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
                            cap_min=config.cap_min, cap_max=config.cap_max,
                            maker_close_rebalance=getattr(config, 'maker_close_rebalance', False))
    gates = GateChain([
        # 并发上限 = eff_concurrency（spec 2026-07-18-margin-gate-exchange-im）：
        # frac 按启用 offset 数 N 放大单格 cap 后，上限必须同步收紧到 N，否则 >N 格
        # 同开会冲破 AL。空启用集 = max_concurrent（零行为变更）。
        MaxConcurrentGate(executor.grids,
                          getattr(config, 'eff_concurrency', config.max_concurrent)),
        # cap 定稿必须在"吃 cap"的门（RiskBudget/MinNotional/Margin）之前（spec 2026-07-15 §五）
        FuseCoverageGate(executor, config.fuse_min_coverage, adapter=adapter,
                         log=_flush_log),
        RiskBudgetGate(executor.grids, config.total_budget, config.default_cap),
        MinNotionalGate(executor, config.min_order_notional, adapter=adapter,
                        log=_flush_log),
        MarginGate(adapter, config.default_cap, executor=executor, log=_flush_log,
                   k=getattr(config, 'margin_gate_k', 1.25)),
    ], log=_flush_log, on_reject=_gate_reject_audit(store))
    bus = EventBus()
    # 实盘退出信号：pv_spike（对齐回测 calc_pv_spike）+ funding_rate（HL 真实费率），按 grid 节流
    signals = LiveSignalProvider(adapter, mult=DEFAULT_STOP_CFG['pv_mult'],
                                 period=DEFAULT_STOP_CFG['pv_period'], n=DEFAULT_STOP_CFG['pv_n'],
                                 refresh_sec=config.signal_refresh_sec, log=_flush_log)
    manager = GridManager(executor, gates, stop_cfg=DEFAULT_STOP_CFG,
                          event_bus=bus, signal_provider=signals)

    sc = DEFAULT_STRATEGY_CONFIG
    label_feed = None
    # tickSize 表：读 inner（未经 ResilientAdapter 包装）——ResilientAdapter 逐方法显式
    # 转发、无 __getattr__（同文件注释：漏转发会静默落到基类默认，2026-07-12 mainnet
    # 实证过 fetch_max_leverages 同款坑）；fetch_tick_sizes 目前未被转发，若在
    # ResilientAdapter 包装后的 `adapter` 上 getattr，生产 ccxt 也会拿到 None，整个
    # tick 过滤在实盘形同虚设。直接读 inner：Fake/HL 遗留没有此方法 → None（过滤
    # 自动关，fail-open）；ccxt 有 → 命中真实实现（本地缓存 markets，零 REST 权重，
    # 不需要重试/熔断，同 quantize_amount/assert_account_mode 的旁路先例）。
    # ⚠ "零权重"这句话是有前提的：它依赖 scheduler 每轮先跑 resolve_live_universe→
    # list_instruments()，把 ccxt markets 缓存暖好，fetch_tick_sizes 内部的
    # load_markets() 才真正是缓存命中、零网络往返。若调度顺序被重排，或未来有人
    # 绕开 scheduler 独立驱动 select_fn/_tick_fn（脱离本轮 universe 解析先跑一步的
    # 前提），这个"零权重"假设不成立，第一次调用可能触发真实 load_markets() 网络请求。
    _tick_fn = getattr(inner, 'fetch_tick_sizes', None)
    if getattr(config, 'selection_ranker', 'rank') == 'eff1':
        from gridtrade.runtime.label_feed import LabelFeed
        from gridtrade.execution.triggers import build_eff1_select_fn
        label_feed = LabelFeed(adapter, pace_ms=config.label_fetch_pace_ms,
                               log=_flush_log)
        _sel = build_eff1_select_fn(sc, label_feed, tick_map_fn=_tick_fn,
                                    min_ticks=config.selection_min_ticks,
                                    log=_flush_log)
        trigger = ScheduledSelectionTrigger(
            sc, ('p12_eff', 'p12_cross1', 'p12_mae'), (), select_fn=_sel)
    else:
        from gridtrade.execution.triggers import _default_select_fn
        _sel = _default_select_fn(sc, sc['factors'], sc['weight_list'],
                                  tick_map_fn=_tick_fn,
                                  min_ticks=config.selection_min_ticks,
                                  log=_flush_log)
        trigger = ScheduledSelectionTrigger(sc, sc['factors'], sc['weight_list'],
                                            select_fn=_sel)
    trigger_engine = TriggerEngine([trigger])

    from gridtrade.state.control import (ControlFlagRepository, CommandRepository,
                                        AuditRepository)
    from gridtrade.runtime.wechat import WeChatNotifier
    notifier = WeChatNotifier(
        config.wechat_webhook_url, store, executor, adapter,
        strategy_name=config.strategy_name, timezone_name=config.wechat_timezone,
        log=_flush_log)
    bus.subscribe(notifier)
    return Runtime(
        config=config, adapter=adapter, store=store, executor=executor,
        manager=manager, trigger_engine=trigger_engine,
        reconciler=Reconciler(executor),
        heartbeats=HeartbeatRepository(store), event_bus=bus,
        flags=ControlFlagRepository(store), commands=CommandRepository(store),
        audit=AuditRepository(store),
        equity=EquitySnapshotRepository(store),
        notifier=notifier,
        label_feed=label_feed,
    )
