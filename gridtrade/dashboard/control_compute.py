"""候选币池排名 + 单币开仓默认参数：复用 trigger_engine 的金标选币管线，只读不下单。"""
import time
from collections import Counter

import pandas as pd

from gridtrade.config import DEFAULT_STRATEGY_CONFIG, DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import capped_symbols
from gridtrade.execution.triggers import TriggerContext
from gridtrade.runtime.scheduler import fetch_universe_candles
from gridtrade.runtime.universe import resolve_live_universe


def compute_proposals(runtime, *, now_fn=time.time, fetch_candles=None):
    rt = runtime
    fetch = fetch_candles or fetch_universe_candles
    run_time = pd.Timestamp(now_fn(), unit='s').floor('H')
    universe = resolve_live_universe(rt.adapter, rt.config.blacklist,
                                     rt.config.whitelist, rt.config.min_quote_volume_24h)
    # 方案A 同口径（经共享 tier_policy，spec 同源性②）：触顶币剔出预览票池（预览语境
    # 无"正在关"的换仓 tag，故不设豁免），预览结果与此刻真正能开的集合一致。
    # manager 缺失（纯只读 runtime）时跳过。
    mgr = getattr(rt, 'manager', None)
    if mgr is not None:
        held = Counter(g.symbol for g in mgr.executor.grids.list_active())
        try:
            _mlmap = rt.adapter.fetch_max_leverages()
        except Exception:
            _mlmap = {}
        universe = [s for s in universe
                    if s not in capped_symbols(universe, held, DEFAULT_TIER_POLICY, maxlev_map=_mlmap)]
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
