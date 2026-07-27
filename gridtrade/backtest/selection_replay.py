"""选币回放（Live/Backtest parity + point-in-time）。复用 gridtrade.core.selection 的实盘选币纯函数。
构造每个 run_time 的 symbol_candle_data 时严格只用 candle_begin_time < run_time 的 bar（纯 UTC）、
取最近 max_candle_num 根，与实盘截断口径一致。
"""
import contextlib
import math
import os
import time

import numpy as np
import pandas as pd

from gridtrade.core.grid_params import GRID_ROW_FACTORS
from gridtrade.core.selection import (compute_offset, needed_factors,
                                      proceed_calc_symbol_factor, select_grid_coin)
from gridtrade.core.tick_fit import filter_tick_fit
from gridtrade.exchanges.base import CANDLE_COLS


def load_full_series(cache, symbols, timeframe='1h'):
    series = {}
    for s in symbols:
        df = cache.read_all_days(timeframe, s)
        if df is None or df.empty:
            continue
        df = df[CANDLE_COLS].copy()
        df.sort_values('candle_begin_time', inplace=True)
        df.drop_duplicates(subset=['candle_begin_time'], keep='last', inplace=True)
        df.reset_index(drop=True, inplace=True)
        series[s] = df
    return series


def build_pit_candidates(series, run_time, *, max_candle_num,
                         min_quote_volume=0.0, top_volume_pct=0.0, blacklist=()):
    """逐 run_time 构造候选 K 线字典：PIT 截断(<run_time) + ≥24 根 + 成交额过滤 + 黑名单。
    成交额两口径可叠加（先地板后相对，与 live resolve_live_universe 同语义，spec
    2026-07-14-universe-top-volume-pct）：24h 量 = 前置 24 根 1h bar 的 quote_volume 之和
    （live 24h ticker 的缓存重建近似）；相对口径取前 ceil(pct×N)，量并列按 symbol 字典序。"""
    bl = set(blacklist)
    eligible = {}                                     # s -> (sub, vol24)
    for s, df in series.items():
        if s in bl:                                   # 档0：无条件硬禁
            continue
        sub = df[df['candle_begin_time'] < run_time]  # PIT，无未来函数
        if len(sub) < 24:
            continue
        vol24 = float(sub.tail(24)['quote_volume'].sum())
        if min_quote_volume and min_quote_volume > 0:  # PIT 绝对成交额地板
            if vol24 < min_quote_volume:
                continue
        eligible[s] = (sub, vol24)
    if top_volume_pct and top_volume_pct > 0 and eligible:  # PIT 相对口径：跨币当轮排名
        keep_n = max(1, math.ceil(float(top_volume_pct) * len(eligible)))
        ranked = sorted(eligible.items(), key=lambda kv: (-kv[1][1], kv[0]))
        eligible = dict(ranked[:keep_n])
    return {s: sub.tail(max_candle_num).copy() for s, (sub, _v) in eligible.items()}


def _rank_eff1(all_df, run_time, choose_symbols, labels, strategy_config, min_ticks,
               tick_map):
    """eff1 排名 —— 与实盘 `triggers.build_eff1_select_fn` **同口径**(动一边必须同步另一边)。

    ①票池只要布网列有限(不走 rank_sum 的 filter v1.0 / 因子 dropna,用户令 2026-07-25);
    ②tick 过滤在排名截断**之前**(名次递补免费);
    ③按 (p12_eff 降序, symbol 升序) 定序取前 choose_symbols —— tiebreak 与回测 make_picks 同。
    缺标签的币直接不参选(实盘是 inner merge,这里是 map 取不到即跳过)。
    """
    d = all_df[np.isfinite(all_df['close']) & np.isfinite(all_df['Atr_5'])
               & np.isfinite(all_df['middle_5'])]
    if min_ticks > 0 and tick_map:
        d, _dropped = filter_tick_fit(d, tick_map, strategy_config, min_ticks, log=None)
    if d.empty or not labels:
        return None
    eff = [labels.get((run_time, s)) for s in d['symbol']]
    d = d.assign(p12_eff=eff)
    d = d[d['p12_eff'].notna()]
    if d.empty:
        return None
    d = d.sort_values(['time', 'p12_eff', 'symbol'], ascending=[True, False, True])
    d = d.assign(rank=d.groupby('time', sort=False).cumcount() + 1.0)
    return d[d['rank'] <= choose_symbols]


def _select_over_run_times(series, run_times, period, weight_list, factors,
                           choose_symbols, max_candle_num, min_quote_volume, blacklist,
                           top_volume_pct=0.0, on_emit=None, on_rt_done=None,
                           ranker='rank', labels=None, min_ticks=0.0, tick_map=None,
                           strategy_config=None):
    """逐 run_time 选币的纯循环体（串行/并行共用）。返回 [(run_time, offset, row)]。
    内部 redirect_stdout 抑制 core 选币函数的诊断 print（no data/[警告] 等）。
    on_emit(rt,off,row) 每选中一条即回调（流式，供断点续跑落盘）；on_rt_done(rt) 每轮**处理完**
    （含无选中的轮）回调一次，标记该轮已完成。默认 None 时行为同旧（仅积累返回）。

    ranker='rank'（rank_sum 加权名次，历史口径逐位不变）或 'eff1'（p12_eff 降序，与实盘
    SELECTION_RANKER 同名同义）。eff1 需 labels={(rt,symbol): p12_eff}（见 p12_replay）。
    min_ticks>0 且给了 tick_map 时在排名前做 tick 过滤（与实盘同位置）。"""
    out = []
    # 只算被引用的因子列(选中结果与全算 diff==0):选币读的 ∪ 布网几何读的(Atr_5/middle_5)
    # eff1 不读打分因子,只需布网列(与实盘 build_eff1_select_fn 的 needed 一致)。
    needed = (set(GRID_ROW_FACTORS) if ranker == 'eff1'
              else needed_factors(factors) | set(GRID_ROW_FACTORS))
    devnull = open(os.devnull, 'w')
    try:
        for run_time in run_times:
            run_time = pd.Timestamp(run_time)
            offset = compute_offset(run_time, period)
            symbol_candle_data = build_pit_candidates(
                series, run_time, max_candle_num=max_candle_num,
                min_quote_volume=min_quote_volume, top_volume_pct=top_volume_pct,
                blacklist=blacklist)
            if symbol_candle_data:
                with contextlib.redirect_stdout(devnull):
                    all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time, period,
                                                        offset, needed=needed, batch=True)
                    factor_data = None
                    if all_df is not None and not all_df.empty:
                        if ranker == 'eff1':
                            factor_data = _rank_eff1(all_df, run_time, choose_symbols,
                                                     labels or {}, strategy_config,
                                                     min_ticks, tick_map)
                        else:
                            factor_data = select_grid_coin(all_df, factors, weight_list,
                                                           choose_symbols, run_time)
                if factor_data is not None:
                    factor_data = factor_data[
                        (factor_data['time'] + pd.to_timedelta(period)) >= run_time]
                    for _, row in factor_data.iterrows():
                        item = (run_time, offset, row.copy())
                        out.append(item)
                        if on_emit is not None:
                            on_emit(*item)
            if on_rt_done is not None:
                on_rt_done(run_time)
    finally:
        devnull.close()
    return out


def _split_contiguous(items, n):
    """把有序列表切成 n 段连续、近等长的子列表（保序；空段丢弃）。"""
    if not items:
        return []
    n = max(1, min(n, len(items)))
    k, m = divmod(len(items), n)
    out, i = [], 0
    for j in range(n):
        sz = k + (1 if j < m else 0)
        if sz:
            out.append(items[i:i + sz])
        i += sz
    return out


def _replay_chunk(payload):
    """进程池 worker（顶层、可 pickle）：各自从本地缓存载 series 后选自己那段 run_time。

    ⚠ payload 尾部是**一个 opts dict**,不是继续加位置项:位置元组每加一项就要在
    「打包 / 解包 / 调用」三处同步改,漏一处就是静默串位(拿 min_ticks 当 top_volume_pct 用)。
    新选项一律进 opts,位次永远冻结在前 12 位。
    """
    (cache, symbols, run_times_chunk, timeframe, period, weight_list, factors,
     choose_symbols, max_candle_num, min_quote_volume, blacklist, top_volume_pct,
     opts) = payload
    series = load_full_series(cache, symbols, timeframe)
    return _select_over_run_times(series, run_times_chunk, period, weight_list, factors,
                                  choose_symbols, max_candle_num, min_quote_volume, blacklist,
                                  top_volume_pct=top_volume_pct, **opts)


def replay_selection(cache, symbols, run_times, strategy_config, factors, on_select, *,
                     timeframe='1h', min_quote_volume=0.0, top_volume_pct=0.0,
                     blacklist=(), workers=1, log=print, on_rt_done=None,
                     ranker='rank', min_ticks=0.0, tick_map=None):
    """ranker='rank'(历史口径)或 'eff1'(与实盘 SELECTION_RANKER 同名同义)。

    eff1 时本函数**自己**按 run_times 从 1m 归档建标签(p12_replay.build_p12_labels),
    调用方不必预先算——保证任何入口(sweep/backtest_run/研究脚本)拿到的都是同一套标签。
    """
    period = strategy_config['period']
    weight_list = strategy_config['weight_list']
    choose_symbols = strategy_config['choose_symbols']
    max_candle_num = strategy_config['max_candle_num']
    if len(weight_list) != len(factors):
        log('[SR][WARN] weight_list(%d)!=factors(%d), 用等权' % (len(weight_list), len(factors)))
        weight_list = [1] * len(factors)

    run_times = list(run_times)
    labels = None
    if ranker == 'eff1':
        from gridtrade.backtest.p12_replay import build_p12_labels
        labels = build_p12_labels(cache, symbols, run_times, log=log)
        if not labels:
            log('[SR][WARN] eff1 标签为空 —— 本段无 1m 归档? 该段将选不出任何币')
    if ranker == 'eff1' and min_ticks > 0 and not tick_map:
        # 与实盘 build_eff1_select_fn 的 WARN 同义:eff1 的回测有效性前提是 MIN_TICKS=3,
        # tick 表缺失=前提被静默关闭,必须响一声(见 memory tick-blindspot-is-eff1-edge)。
        log('[SR][WARN] eff1 + min_ticks=%g 但无 tick 表 —— tick 过滤失效,回测口径与实盘不符'
            % min_ticks)
    opts = {'ranker': ranker, 'labels': labels, 'min_ticks': min_ticks,
            'tick_map': tick_map, 'strategy_config': strategy_config}
    if workers and workers > 1 and len(run_times) > 1:
        from concurrent.futures import ProcessPoolExecutor
        chunks = _split_contiguous(run_times, workers)
        payloads = [(cache, symbols, chunk, timeframe, period, weight_list, factors,
                     choose_symbols, max_candle_num, min_quote_volume, blacklist,
                     top_volume_pct, opts)
                    for chunk in chunks]
        with ProcessPoolExecutor(max_workers=len(payloads)) as ex:
            # map 保输入序 ⇒ 与串行逐位一致;每块回来后标记该块所有轮已完成(块粒度续跑)
            for chunk, chunk_result in zip(chunks, ex.map(_replay_chunk, payloads)):
                for run_time, offset, row in chunk_result:
                    on_select(run_time, offset, row)
                if on_rt_done is not None:
                    for rt in chunk:
                        on_rt_done(pd.Timestamp(rt))
    else:
        series = load_full_series(cache, symbols, timeframe)
        # 串行流式:on_emit 逐条产出、on_rt_done 逐轮标记完成(轮粒度续跑)
        _select_over_run_times(
            series, run_times, period, weight_list, factors,
            choose_symbols, max_candle_num, min_quote_volume, blacklist,
            top_volume_pct=top_volume_pct, on_emit=on_select, on_rt_done=on_rt_done,
            **opts)
    return len(run_times)
