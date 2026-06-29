"""候选币池排名 + 单币开仓默认参数：复用 trigger_engine 的金标选币管线，只读不下单。"""
import time

import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG
from gridtrade.execution.triggers import TriggerContext
from gridtrade.runtime.scheduler import fetch_universe_candles
from gridtrade.runtime.universe import resolve_live_universe


def compute_proposals(runtime, *, now_fn=time.time, fetch_candles=None):
    rt = runtime
    fetch = fetch_candles or fetch_universe_candles
    run_time = pd.Timestamp(now_fn(), unit='s').floor('H')
    universe = resolve_live_universe(rt.adapter, rt.config.blacklist, rt.config.whitelist)
    candles = fetch(rt.adapter, universe, run_time,
                    max_candle_num=DEFAULT_STRATEGY_CONFIG['max_candle_num'])
    ctx = TriggerContext(rt.config.exchange, run_time, candles)
    out = []
    for p in rt.trigger_engine.collect(ctx):
        out.append({'symbol': p.symbol, 'grid_params': dict(p.grid_params),
                    'tag': getattr(p, 'tag', ''), 'offset': getattr(p, 'offset', 0)})
    return out


def defaults_for_symbol(runtime, symbol, *, now_fn=time.time, fetch_candles=None):
    for p in compute_proposals(runtime, now_fn=now_fn, fetch_candles=fetch_candles):
        if p['symbol'] == symbol:
            return p
    return None
