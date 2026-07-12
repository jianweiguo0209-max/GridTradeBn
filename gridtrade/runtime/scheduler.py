"""scheduler 机入口（常驻 process group）：每整点关旧 tag → 选币 → 准入 → 开新 → 心跳。

默认仅整点跑（避免部署 mid-hour 重处理当前 offset 而 churn）；SCHEDULER_RUN_ON_START
=true 时启动立即跑一次（testnet 调试）。单轮异常降级续跑不退出，SIGTERM 优雅停。
"""
import signal
import time
from collections import Counter

import pandas as pd

from gridtrade.config import (DEFAULT_STRATEGY_CONFIG, DEFAULT_TIER_POLICY,
                              load_deploy_config)
from gridtrade.core.selection import compute_offset
from gridtrade.core.tier_policy import capped_symbols
from gridtrade.execution.triggers import TriggerContext
from gridtrade.runtime.shock import cross_median_k
from gridtrade.runtime.cycles import braked_symbols, run_scheduler_cycle
from gridtrade.runtime.factory import build_runtime
from gridtrade.runtime.introspect import adapter_endpoint
from gridtrade.runtime.universe import resolve_live_universe


# 逐币取数间隔（ms）。HL 权重制限频：IP 预算 1200/分，candleSnapshot 权重 20 + 每 60 根加权
# （160 根 ≈ 23/币）；2000ms → 30 请求/分 ≈ 690 权重（58% 预算），给 monitor 留近半余量。
# 全市场 ~91 币一轮 ≈ 3.5 分钟（选币 K 线截止仍锚定整点 run_time，仅下单顺延几分钟）。
# ccxt enableRateLimit 的 50ms 名义间隔对权重制无效（2026-07-05 05:00 mainnet 429 风暴实证）。
FETCH_PACE_MS_DEFAULT = 2000.0


def fetch_universe_candles(adapter, symbols, run_time, *, timeframe='1h',
                           max_candle_num=160, pace_ms=None, sleep=time.sleep) -> dict:
    end_ms = int(pd.Timestamp(run_time).timestamp() * 1000)
    start_ms = end_ms - max_candle_num * 3600 * 1000   # 1h 根
    if pace_ms is None:
        pace_ms = FETCH_PACE_MS_DEFAULT
    out = {}
    skipped = 0
    first_err = None
    for i, sym in enumerate(symbols):
        if i and pace_ms > 0:
            sleep(pace_ms / 1000.0)   # 币间节流（默认开；env SCHEDULER_FETCH_PACE_MS 可调，0=关）
        try:
            df = adapter.fetch_ohlcv(sym, timeframe, start_ms, end_ms)
        except Exception as exc:
            skipped += 1            # 坏币（BadSymbol/无数据/拉取失败）跳过，不阻塞整池
            if first_err is None:
                first_err = '%s -> %r' % (sym, exc)
            continue
        if df is not None and not df.empty:
            out[sym] = df
    if skipped:
        print('[scheduler] skipped %d symbols (e.g. %s)' % (skipped, first_err),
              flush=True)
    return out


def run_scheduler_once(runtime, *, now_fn=time.time,
                       fetch_candles=fetch_universe_candles) -> dict:
    rt = runtime
    flags = getattr(rt, 'flags', None)
    if flags is not None:
        if flags.get('trading_halted'):
            rt.heartbeats.beat('scheduler')
            return {'skipped': 'halted'}
        if flags.get('scheduler_paused'):
            rt.heartbeats.beat('scheduler')
            return {'skipped': 'paused'}
    run_time = pd.Timestamp(now_fn(), unit='s').floor('H')
    period = rt.config.scheduler_period
    offset = compute_offset(run_time, period)
    tag = '%s%d' % (DEFAULT_STRATEGY_CONFIG['strategy_tag'], offset)
    universe = resolve_live_universe(rt.adapter, rt.config.blacklist,
                                     rt.config.whitelist, rt.config.min_quote_volume_24h)
    # 方案A（legacy 半拉黑档2 执行位对齐）经共享 tier_policy 表达（spec 同源性②，
    # 与回测 allocate_with_tiers 同一判定源）：触顶币在选币入口剔出票池——连 K 线都
    # 不拉，因子排名自动落次优币；否则榜一触顶会在开仓时被 DB 槽位拒、当轮空转
    # （testnet SOL×2/HYPE 实证）。本轮换仓 tag 自己持有的币即将被 close_tag 释放
    # → 不计 held（允许连任，状态供给侧口径）。竞态守卫=GridRepository 槽位 UNIQUE
    # （SlotExhausted 由 open_proposals 逐提议捕获，SymbolLockGate 已删）。
    braked = braked_symbols(flags)            # 外部干预熔断币(spec 2026-07-12 组件三)
    if braked:
        universe = [s for s in universe if s not in braked]
        print('[intervention] scheduler pre-filter: -%d braked %s'
              % (len(braked), sorted(braked)), flush=True)
    held = Counter(g.symbol for g in rt.manager.executor.grids.list_active()
                   if g.tag != tag)
    try:
        _mlmap = rt.adapter.fetch_max_leverages()   # 杠杆感知上限(组件四);失败 fail-open
    except Exception:
        _mlmap = {}
    banned = capped_symbols(universe, held, DEFAULT_TIER_POLICY, maxlev_map=_mlmap)
    if banned:
        universe = [s for s in universe if s not in banned]
        print('[scheduler] symbol-lock pre-filter: -%d held %s'
              % (len(banned), sorted(banned)), flush=True)
    candles = fetch_candles(rt.adapter, universe, run_time,
                            max_candle_num=DEFAULT_STRATEGY_CONFIG['max_candle_num'],
                            pace_ms=getattr(rt.config, 'scheduler_fetch_pace_ms', None))
    # 票池快照(2026-07-12,选币可复现性):落"实际进入排名的集合"(post 地板/黑名单/
    # held 预过滤/braked/取数跳过)——因子名次是组内相对名次,没有它历史选币不可精确
    # 复现(实证:TRUMP 在 168 币集合无影、57 币线上集合进 #4)。fail-soft:快照失败
    # 绝不阻塞选币开格。
    try:
        from gridtrade.state.universe_snapshots import UniverseSnapshotRepository
        UniverseSnapshotRepository(rt.store).add(
            rt.config.exchange, int(run_time.value // 1_000_000),
            list(candles.keys()),
            excluded={'held_banned': sorted(banned), 'braked': sorted(braked)})
    except Exception as exc:
        print('[scheduler] universe snapshot skipped: %r' % exc, flush=True)
    # MarketShockBrake(spec 2026-07-08):|票池中位数 k 小时收益|≥thr → 本轮只关不开,
    # 并暂停 pause 小时;状态进程内(信号自持 ~k 小时,重启自愈,约束 pause<=k)。
    open_enabled = True
    thr = float(getattr(rt.config, 'shock_thr', 0.0) or 0.0)
    if thr > 0:
        k = int(getattr(rt.config, 'shock_k_hours', 4))
        pause = int(getattr(rt.config, 'shock_pause_hours', 2))
        if pause > k:
            print('[shock] WARN pause(%dh)>k(%dh):重启窗口可能漏暂停' % (pause, k), flush=True)
        med = cross_median_k(candles, run_time, k)
        until = getattr(rt, '_shock_until', None)
        if med is not None and abs(med) >= thr:
            new_until = run_time + pd.Timedelta(hours=pause)
            if until is None or new_until > until:
                until = new_until
                setattr(rt, '_shock_until', until)
            print('[shock] med_%dh=%+.1f%% |>=%.1f%%| -> 暂停开格至 %s'
                  % (k, 100 * med, 100 * thr, until), flush=True)
        if until is not None and run_time < until:
            open_enabled = False
            if med is None or abs(med) < thr:
                print('[shock] braked until %s(信号已回落,窗口内继续暂停)' % until, flush=True)
    ctx = TriggerContext(rt.config.exchange, run_time, candles)
    result = run_scheduler_cycle(rt.manager, rt.trigger_engine, rt.reconciler,
                                 ctx, close_tag=tag, open_enabled=open_enabled,
                                 braked_symbols=frozenset(braked))
    rt.heartbeats.beat('scheduler')
    return result


def _seconds_to_next_hour(now_epoch) -> int:
    return 3600 - (int(now_epoch) % 3600)


def _safe_run(runtime, run_once_fn, now_fn, log):
    try:
        run_once_fn(runtime, now_fn=now_fn)
    except Exception as exc:          # 降级：记录 + 续跑，绝不退出
        log('[scheduler] degraded: %r' % exc)


def run_scheduler(runtime, *, once=False, sleep=time.sleep, now_fn=time.time,
                  log=print, run_once_fn=run_scheduler_once,
                  should_stop=None, run_on_start=False):
    if run_on_start:
        _safe_run(runtime, run_once_fn, now_fn, log)
        if once:
            return
    while True:
        sleep(_seconds_to_next_hour(now_fn()))
        _safe_run(runtime, run_once_fn, now_fn, log)
        if once:
            return
        if should_stop is not None and should_stop():
            return


def main() -> None:   # composition root（不单测）
    rt = build_runtime(load_deploy_config())
    print('[scheduler] exchange=%s testnet=%s endpoint=%s run_on_start=%s period=%s'
          % (rt.config.exchange, rt.config.testnet, adapter_endpoint(rt.adapter),
             rt.config.scheduler_run_on_start, rt.config.scheduler_period),
          flush=True)
    stop = {'flag': False}

    def _graceful(signum, frame):
        stop['flag'] = True

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    run_scheduler(rt, should_stop=lambda: stop['flag'],
                  run_on_start=rt.config.scheduler_run_on_start)


if __name__ == '__main__':
    main()
