"""scheduler 机入口（scale-to-zero 一次性）：关旧 tag → 选币 → 准入 → 开新 → 心跳。"""
import time

import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG, load_deploy_config
from gridtrade.core.selection import compute_offset
from gridtrade.execution.triggers import TriggerContext
from gridtrade.runtime.cycles import run_scheduler_cycle
from gridtrade.runtime.factory import build_runtime
from gridtrade.runtime.universe import resolve_live_universe


def fetch_universe_candles(adapter, symbols, run_time, *, timeframe='1H',
                           max_candle_num=160) -> dict:
    end_ms = int(pd.Timestamp(run_time).timestamp() * 1000)
    start_ms = end_ms - max_candle_num * 3600 * 1000   # 1H 根
    out = {}
    for sym in symbols:
        df = adapter.fetch_ohlcv(sym, timeframe, start_ms, end_ms)
        if df is not None and not df.empty:
            out[sym] = df
    return out


def run_scheduler_once(runtime, *, now_fn=time.time,
                       fetch_candles=fetch_universe_candles) -> dict:
    rt = runtime
    run_time = pd.Timestamp(now_fn(), unit='s').floor('H')
    period = rt.config.scheduler_period
    offset = compute_offset(run_time, period, rt.config.utc_offset)
    tag = '%s%d' % (DEFAULT_STRATEGY_CONFIG['strategy_tag'], offset)
    universe = resolve_live_universe(rt.adapter, rt.config.blacklist)
    candles = fetch_candles(rt.adapter, universe, run_time,
                            max_candle_num=DEFAULT_STRATEGY_CONFIG['max_candle_num'])
    ctx = TriggerContext(rt.config.exchange, run_time, candles)
    result = run_scheduler_cycle(rt.manager, rt.trigger_engine, rt.reconciler,
                                 ctx, close_tag=tag)
    rt.heartbeats.beat('scheduler')
    return result


def main() -> None:   # composition root（不单测）
    rt = build_runtime(load_deploy_config())
    out = run_scheduler_once(rt)
    print('[scheduler] closed=%d opened=%d' % (len(out['closed']),
                                               len(out['opened'])))
