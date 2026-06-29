"""scheduler 机入口（常驻 process group）：每整点关旧 tag → 选币 → 准入 → 开新 → 心跳。

默认仅整点跑（避免部署 mid-hour 重处理当前 offset 而 churn）；SCHEDULER_RUN_ON_START
=true 时启动立即跑一次（testnet 调试）。单轮异常降级续跑不退出，SIGTERM 优雅停。
"""
import signal
import time

import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG, load_deploy_config
from gridtrade.core.selection import compute_offset
from gridtrade.execution.triggers import TriggerContext
from gridtrade.runtime.cycles import run_scheduler_cycle
from gridtrade.runtime.factory import build_runtime
from gridtrade.runtime.introspect import adapter_endpoint
from gridtrade.runtime.universe import resolve_live_universe


def fetch_universe_candles(adapter, symbols, run_time, *, timeframe='1h',
                           max_candle_num=160) -> dict:
    end_ms = int(pd.Timestamp(run_time).timestamp() * 1000)
    start_ms = end_ms - max_candle_num * 3600 * 1000   # 1h 根
    out = {}
    skipped = 0
    first_err = None
    for sym in symbols:
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
    offset = compute_offset(run_time, period, rt.config.utc_offset)
    tag = '%s%d' % (DEFAULT_STRATEGY_CONFIG['strategy_tag'], offset)
    universe = resolve_live_universe(rt.adapter, rt.config.blacklist,
                                     rt.config.whitelist)
    candles = fetch_candles(rt.adapter, universe, run_time,
                            max_candle_num=DEFAULT_STRATEGY_CONFIG['max_candle_num'])
    ctx = TriggerContext(rt.config.exchange, run_time, candles)
    result = run_scheduler_cycle(rt.manager, rt.trigger_engine, rt.reconciler,
                                 ctx, close_tag=tag)
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
