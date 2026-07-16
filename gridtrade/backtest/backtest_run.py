"""端到端回测：选币回放 → 布网 → 持仓 bars → simulate_grid_engine → 聚合。
全部复用 gridtrade.core 纯函数；数据从 ParquetCache 读（预热后离线）。

本模块是**唯一预热+回测入口**：
  - run_backtest(...)         纯离线核心（只读 cache，无网络）——测试/复用都走它。
  - prewarm_1h(...)           phase1：全市场(−黑名单) 1h 选币 OHLCV(Vision 归档+API 尾补)，幂等按天缓存复用。
  - prewarm_sim_and_funding() phase2：仅选中币 持仓K线+funding(Vision 归档+API 尾补)。
  - main()                    CLI：全市场解析+1h预热 → select_grids(一次) → 仅选中币预热 → 回测 → summarize → CSV。
                              跑：TZ=Asia/Shanghai .venv/bin/python -m gridtrade.backtest.backtest_run [days] [sim_tf]
网络依赖（ccxt/adapter/vision）全部在 prewarm_1h/prewarm_sim_and_funding/main 内**惰性导入**，保持核心离线可测。
"""
import heapq
import os

from gridtrade.backtest._resource_guard import apply_thread_caps, safe_workers

apply_thread_caps()  # 必须在 import pandas(→numpy/OpenBLAS)前锁每进程线程，防多进程超订

import pandas as pd

from gridtrade.backtest import selection_replay as SR
from gridtrade.backtest.cache import ParquetCache
from gridtrade.config import DEFAULT_STOP_CFG, DEFAULT_STRATEGY_CONFIG
from gridtrade.core.grid_engine import calc_pv_spike, simulate_grid_engine
from gridtrade.core.grid_params import calc_grid_params_v1, calc_grid_params_v2
from gridtrade.exchanges.binance import is_coin_market

# 回测默认策略（镜像 gridtrade.config 已验证参数；stop_loss_config 补 funding 止损）
BT_STRATEGY = {**DEFAULT_STRATEGY_CONFIG,
               'stop_loss_config': dict(DEFAULT_STOP_CFG),
               'active_stop_mode': 'pv',   # 主动止损默认 pv（回测扫描最优）
               'pv_config': {'mult': DEFAULT_STOP_CFG['pv_mult'], 'pnl_thr': DEFAULT_STOP_CFG['pv_pnl_thr'],
                             'period': DEFAULT_STOP_CFG['pv_period'], 'n': DEFAULT_STOP_CFG['pv_n']}}
BT_FACTORS = dict(DEFAULT_STRATEGY_CONFIG['factors'])

# 回测票池口径对齐 prod：全市场动态 −黑名单 −逐 run_time PIT 24h 成交额**前 55% 相对口径**
# （spec 2026-07-14-universe-top-volume-pct；绝对地板机制保留可叠加，默认 0=停用）。
# 票池在 main() 里由 vision.list_archive_symbols 解析全市场（归档含退市，见 main）；此处只放阈值/黑名单常量（可 env 覆写）。
BT_MIN_QUOTE_VOLUME_24H = float(os.environ.get('BT_MIN_QUOTE_VOLUME_24H', '0'))
BT_UNIVERSE_TOP_PCT = float(os.environ.get('BT_UNIVERSE_TOP_PCT', '0.55'))
BT_BLACKLIST = tuple(s.strip() for s in os.environ.get('BT_BLACKLIST', '').split(',') if s.strip())


def _weights_from_env(strategy_config, factors):
    """选币权重覆盖 env（P1 权重扫描研究用；spec 2026-07-07 正确管线重跑）：
    BT_WEIGHTS='w_Reg,w_Sgcz,w_Er'（对齐 factors dict 顺序）覆盖 rank 权重；
    BT_SGCZ_DESC=1 把 Sgcz 方向翻转（ascending False）。未设 → 原样返回（现行等权）。
    只改选币排名组合，不动因子值/其它策略参数。"""
    wv = os.environ.get('BT_WEIGHTS')
    sc, fac = strategy_config, factors
    if wv:
        sc = dict(strategy_config,
                  weight_list=[float(x) for x in wv.split(',') if x.strip() != ''])
    if os.environ.get('BT_SGCZ_DESC', '').lower() in ('1', 'true', 'on'):
        fac = dict(factors); fac['Sgcz_5'] = False
    return sc, fac


def _tiers_from_env():
    """三档评估 env（spec 2026-07-06-tiered-*）：显式设置任一 BT_TIER* 才启用
    （默认 None=基线可比）；未给的档位回落 DEFAULT_TIER_POLICY（名单单源 config.py）。"""
    t0v = os.environ.get('BT_TIER0')
    t1v = os.environ.get('BT_TIER1_SYMBOLS')
    t2v = os.environ.get('BT_TIER2_CAP')
    if t0v is None and t1v is None and t2v is None:
        return None
    from gridtrade.config import DEFAULT_TIER_POLICY
    from gridtrade.core.tier_policy import TierPolicy
    _csv = lambda v: tuple(x.strip() for x in v.split(',') if x.strip())
    return TierPolicy(
        tier0=_csv(t0v) if t0v is not None else DEFAULT_TIER_POLICY.tier0,
        tier1=_csv(t1v) if t1v is not None else DEFAULT_TIER_POLICY.tier1,
        tier2_cap=int(t2v) if t2v is not None else DEFAULT_TIER_POLICY.tier2_cap)


def holding_bars(series_df, run_time, period):
    td = pd.to_timedelta(period)
    cbt = series_df['candle_begin_time']
    sub = series_df[(cbt >= run_time) & (cbt < run_time + td)]
    return sub.sort_values('candle_begin_time')


def _funding_missing(funding_df, bars_df):
    if funding_df is None or funding_df.empty or len(bars_df) == 0:
        return True
    lo = bars_df['candle_begin_time'].min()
    hi = bars_df['candle_begin_time'].max()
    fts = pd.to_datetime(funding_df['ts'], unit='ms')
    return not ((fts >= lo) & (fts <= hi)).any()


def summarize(df):
    if df.empty:
        return {'n_grids': 0}
    offset_eq = {}
    for off, g in df.sort_values('run_time').groupby('offset'):
        eq = 1.0
        for pr in g['pnl_ratio']:
            eq *= (1.0 + pr)
        offset_eq[int(off)] = eq
    port_return = sum(offset_eq.values()) / len(offset_eq) - 1.0
    return {
        'n_grids': int(len(df)),
        'win_rate': float((df['pnl_ratio'] > 0).mean()),
        'mean_pnl_ratio': float(df['pnl_ratio'].mean()),
        'median_pnl_ratio': float(df['pnl_ratio'].median()),
        'portfolio_return': float(port_return),
        'offset_equity': offset_eq,
        'exit_reasons': df['exit_reason'].value_counts().to_dict(),
    }


# 仿真明细列（顺序须与 _simulate_grid_task 返回 dict 的 key 顺序一致）——
# 零任务时给空 DataFrame 带上 schema，避免下游 df['symbol'] 触 KeyError。
_RESULT_COLS = ['run_time', 'offset', 'symbol', 'entry', 'grid_num', 'low', 'high',
                'hold_bars', 'n_fills', 'pnl_ratio', 'exit_reason', 'terminated',
                'unreal_pnl', 'funding_missing']


# data_task 元组布局（下游按位取用，勿改序；新增字段一律追加在尾部）
TASK_PV_IDX = 7          # pv_spike_df（按 27h 前置历史预算，见 _pv_spike_for_window）


def _simulate_grid_task(payload):
    """单个网格仿真（顶层函数，可 pickle → 供进程池并行）。
    payload=(data_task, cfg)：data_task 是选币/数据（可跨参数组合复用），cfg 是仿真配置。
    funding_df 已在父进程按持仓窗预切片（等价、payload 小）；pv_spike_df 已在父进程按
    27h 前置历史预算（实盘同源语义，见 assemble_grid_tasks）。"""
    (rt, offset, sym, entry, gp, bars_df, funding_df, pv_spike_df), cfg = payload
    pv_cfg = cfg['pv_cfg']
    sim = simulate_grid_engine(bars_df, gp, cap=1000.0, leverage=cfg['lev'], fee=cfg['fee_rate'],
                               c_rate_taker=cfg.get('taker_rate', 0.0005),
                               max_rate=cfg['max_rate'], min_amount=0.0, stop_cfg=cfg['stop_cfg'],
                               funding_df=funding_df, neutral_init=False,
                               pv_spike_df=pv_spike_df,
                               active_stop_mode=cfg['active_stop_mode'],
                               pv_pnl_thr=pv_cfg.get('pnl_thr', -0.015),
                               pv_mult=pv_cfg.get('mult', 3), pv_n=pv_cfg.get('n', 233),
                               pv_period=pv_cfg.get('period', '15min'),
                               pv_body_ratio=pv_cfg.get('con2', 0.0))
    return {
        'run_time': rt, 'offset': int(offset), 'symbol': sym,
        'entry': entry, 'grid_num': int(gp['grid_count']),
        'low': round(gp['low_price'], 8), 'high': round(gp['high_price'], 8),
        'hold_bars': int(len(bars_df)), 'n_fills': int(sim['n_trades']),
        'pnl_ratio': float(sim['pnl_ratio']), 'exit_reason': sim['exit_reason'],
        'terminated': bool(sim['terminated']),
        'unreal_pnl': float(sim.get('unreal_pnl', 0.0)),      # 浮盈拆分（诊断用）
        'funding_missing': bool(_funding_missing(funding_df, bars_df)),
    }


def select_grids(cache, universe, window_start, window_end, strategy_config, factors,
                 *, timeframe='1h', min_quote_volume=0.0, top_volume_pct=0.0,
                 blacklist=(), workers=1, candidates_per_rt=1, log=print):
    """只跑选币回放（1h + PIT 成交额过滤(地板/前 pct 相对口径可叠加) + 黑名单），
    返回 [(rt, offset, row)]。offline。
    结果按选币参数 + 每币缓存天范围数据指纹磁盘缓存（BT_SELECT_CACHE=off 旁路）。
    candidates_per_rt>1：三档递补用 top-K 候选——放宽选币截断为 rank<=K（经
    strategy_config.choose_symbols 覆盖，天然进缓存 key、不同 K 不串）；K=1 逐位恒等现状。"""
    if candidates_per_rt and int(candidates_per_rt) > 1:
        strategy_config = dict(strategy_config, choose_symbols=int(candidates_per_rt))
    from gridtrade.backtest import select_cache as SC
    use_cache = SC.enabled()
    key = params = None
    if use_cache:
        key, params = SC.compute_key(cache, universe, window_start, window_end, timeframe,
                                     min_quote_volume, blacklist, strategy_config, factors,
                                     top_volume_pct=top_volume_pct)
        hit = SC.load(cache, key, params)
        if hit is not None:
            log('[BT] select cache HIT %s (picks=%d)' % (key, len(hit)))
            return hit
    grids = []
    run_times = [pd.Timestamp(t) for t in pd.date_range(window_start, window_end, freq='1H')]
    SR.replay_selection(cache, universe, run_times, strategy_config, factors,
                        lambda rt, off, row: grids.append((rt, off, row.copy())),
                        timeframe=timeframe, min_quote_volume=min_quote_volume,
                        top_volume_pct=top_volume_pct,
                        blacklist=blacklist, workers=workers, log=log)
    log('[BT] picks=%d' % len(grids))
    if use_cache:
        SC.save(cache, key, params, grids)
        log('[BT] select cache MISS %s (saved)' % key)
    return grids


def pv_spike_for_window(series_df, bars_df, pv_cfg):
    """按**实盘同源语义**逐格算 pv 量能尖峰（spec 2026-07-15-binance-param-sweep §0）。

    实盘 LiveSignalProvider 取原生 15m 的 n+8 根（n=100 → ≈27h）算 rolling(n) 基线；回测若
    只喂 12h 持仓窗（48 根 15m、min_periods=1），基线退化为「窗内扩张均值」——开窗头几根
    样本仅 1-2 根、且看不到开仓前量能水位 → 系统性误报尖峰（pv 是最大单项退出驱动，实测
    砍 ~49% 的格）。此处从完整 1m 序列取窗前 (n+8)×15min 前置历史拼进去再算，然后裁回持仓窗。
    sim_tf 非 1m（如 1h）时前置历史根数不足，退化为可用范围——1h 模式本就是粗略口径。"""
    n = int(pv_cfg.get('n', 100))
    lookback = pd.Timedelta(minutes=(n + 8) * 15)
    t0 = bars_df['candle_begin_time'].min()
    pre = series_df[(series_df['candle_begin_time'] < t0)
                    & (series_df['candle_begin_time'] >= t0 - lookback)]
    src = pd.concat([pre, bars_df], ignore_index=True) if len(pre) else bars_df
    sp = calc_pv_spike(src.sort_values('candle_begin_time'),
                       active_period=pv_cfg.get('period', '15min'),
                       mult=pv_cfg.get('mult', 3), n=n,
                       body_ratio_min=pv_cfg.get('con2', 0.0))
    if sp is None:
        return None
    hi = bars_df['candle_begin_time'].max()
    return sp[(sp['candle_begin_time'] >= t0) & (sp['candle_begin_time'] <= hi)].reset_index(drop=True)


def assemble_grid_tasks(cache, grids, strategy_config, *, sim_timeframe=None,
                        timeframe='1h', log=print):
    """由选中 grids 组装每格 data_task（载选中币 sim 序列 + holding_bars + funding 切片
    + **前置历史 pv 尖峰**）。offline。元组布局见 TASK_PV_IDX。"""
    sim_tf = sim_timeframe or timeframe
    period = strategy_config['period']
    price_limit = strategy_config['price_limit']
    stop_limit = strategy_config['stop_limit']
    grid_version = strategy_config.get('grid_version', 1)
    v2cfg = strategy_config.get('grid_v2_config', {})
    pv_cfg = strategy_config.get('pv_config', {})
    calc_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1

    selected = sorted({row['symbol'] for _, _, row in grids})
    series = SR.load_full_series(cache, selected, sim_tf)   # 仅选中币
    funding_by_sym = {}
    data_tasks = []
    for rt, offset, row in grids:
        sym = row['symbol']
        if sym not in series:
            continue
        bars_df = holding_bars(series[sym], rt, period)
        if len(bars_df) == 0:
            continue
        px = calc_fn(row=row, price_limit=price_limit, stop_limit=stop_limit, v2_config=v2cfg)
        gp = dict(low_price=px['low_price'], high_price=px['high_price'],
                  grid_count=px['grid_count'], stop_high_price=px['stop_high_price'],
                  stop_low_price=px['stop_low_price'])
        if sym not in funding_by_sym:
            funding_by_sym[sym] = cache.read_all_days('funding', sym)
        fd = funding_by_sym[sym]
        if fd is not None and not fd.empty:      # 预切到持仓窗（与全量 merge 等价、payload 小）
            lo = int(bars_df['candle_begin_time'].min().value // 1_000_000)
            hi = int(bars_df['candle_begin_time'].max().value // 1_000_000)
            fd = fd[(fd['ts'] >= lo) & (fd['ts'] <= hi)]
        pv_df = pv_spike_for_window(series[sym], bars_df, pv_cfg)
        data_tasks.append((rt, int(offset), sym, float(row['close']), gp, bars_df, fd, pv_df))
    return data_tasks


def build_grid_tasks(cache, universe, window_start, window_end, strategy_config, factors,
                     *, timeframe='1h', sim_timeframe=None, min_quote_volume=0.0,
                     top_volume_pct=0.0, blacklist=(), workers=1, log=print):
    """选币 + 组装（offline 便捷组合，run_backtest/测试用）。两段式预热见 main()。"""
    grids = select_grids(cache, universe, window_start, window_end, strategy_config, factors,
                         timeframe=timeframe, min_quote_volume=min_quote_volume,
                         top_volume_pct=top_volume_pct,
                         blacklist=blacklist, workers=workers, log=log)
    return assemble_grid_tasks(cache, grids, strategy_config,
                               sim_timeframe=sim_timeframe, timeframe=timeframe, log=log)


def simulate_tasks(data_tasks, *, leverage, fee_rate=0.0002, taker_rate=0.0005,
                   max_rate=0.68, stop_cfg=None,
                   active_stop_mode='pv', pv_cfg=None, workers=1):
    """对已组装的 data_tasks 跑仿真（可并行）→ 明细 DataFrame。仿真配置在此传入，故同一批
    data_tasks 可反复用不同 (active_stop_mode/pv_cfg/stop_cfg) 仿真——扫参提速的关键。
    fee_rate=maker（网格挂单成交，默认 2bps）、taker_rate=taker（平仓/止损/破网，默认
    5bps）——对齐币安 USDT-M VIP0 无折扣费率（maker 2bps/taker 5bps，用户定 2026-07-14）。"""
    cfg = {'lev': leverage, 'fee_rate': fee_rate, 'taker_rate': taker_rate,
           'max_rate': max_rate, 'stop_cfg': stop_cfg,
           'active_stop_mode': active_stop_mode, 'pv_cfg': pv_cfg or {}}
    payloads = [(dt, cfg) for dt in data_tasks]
    if workers and workers > 1 and len(payloads) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_simulate_grid_task, payloads, chunksize=8))
    else:
        results = [_simulate_grid_task(p) for p in payloads]
    return pd.DataFrame(results) if results else pd.DataFrame(columns=_RESULT_COLS)


def filter_tasks_symbol_lock(data_tasks, period='12H'):
    """镜像实盘 SymbolLockGate 语义的回测过滤：同币在上一格开仓后 period 锁窗内再被选中
    → 剔除且不递补（该 run_time 该币空过）；恰满 period 边界=锁释放（实盘轮换同刻关旧开新）。
    近似注记：实盘止损提前退出会提前释放锁（四窗实测提前退出仅 ~4% 格），本过滤按固定
    period 锁，偏保守。返回 (保留的 tasks 原对象原顺序, 剔除数)。"""
    td = pd.to_timedelta(period)
    locked_until = {}
    kept = []
    n_rejected = 0
    for task in data_tasks:
        rt, sym = task[0], task[2]
        if sym in locked_until and rt < locked_until[sym]:
            n_rejected += 1
            continue
        locked_until[sym] = rt + td
        kept.append(task)
    return kept, n_rejected


def allocate_with_tiers(ranked_picks, tiers, period='12H'):
    """三档分配（spec 2026-07-06-tiered-*）：按 run_time 升序，每轮候选按 rank 升序经
    共享 pick_first_allowed 取第一个未触顶币（=实盘方案A 次优递补）；held 记账为
    **固定 period 锁窗近似**（恰满边界=释放，与 filter_tasks_symbol_lock 同口径；
    实盘止损早退会提前释放锁，四窗实测早退 ~4% 格，本近似偏保守）。排名在全池上算、
    触顶后跳选（实盘为剔后再排，微小位移方向无偏——保选币向量化的既定取舍）。
    返回 (picks 同形子集, stats)。"""
    from gridtrade.core.tier_policy import pick_first_allowed
    td = pd.to_timedelta(period)
    by_round = {}
    for rt, off, row in ranked_picks:
        by_round.setdefault((rt, off), []).append((rt, off, row))
    expiry = []          # [(release_ts, symbol)] 最小堆
    held = {}
    kept = []
    stats = {'rejected_tier1': 0, 'rejected_tier2': 0,
             'fallback_hist': {}, 'empty_rounds': 0}
    for key in sorted(by_round):
        rt = key[0]
        while expiry and expiry[0][0] <= rt:                 # 恰满边界=释放
            _, sym = heapq.heappop(expiry)
            held[sym] -= 1
            if not held[sym]:
                del held[sym]
        cands = sorted(by_round[key], key=lambda t: t[2]['rank'])
        idx = pick_first_allowed([c[2]['symbol'] for c in cands], held, tiers)
        for j, c in enumerate(cands):                        # idx 之前的候选=被拒（计档）
            if idx is not None and j >= idx:
                break
            sym = c[2]['symbol']
            if sym in tiers.tier1:
                stats['rejected_tier1'] += 1
            else:
                stats['rejected_tier2'] += 1
        if idx is None:
            stats['empty_rounds'] += 1
            continue
        if idx > 0:
            stats['fallback_hist'][idx] = stats['fallback_hist'].get(idx, 0) + 1
        chosen = cands[idx]
        sym = chosen[2]['symbol']
        held[sym] = held.get(sym, 0) + 1
        heapq.heappush(expiry, (rt + td, sym))
        kept.append(chosen)
    return kept, stats


def run_backtest(cache, universe, window_start, window_end, strategy_config, factors,
                 *, timeframe='1h', sim_timeframe=None, fee_rate=0.0002, taker_rate=0.0005,
                 max_rate=0.68, leverage=None, min_quote_volume=0.0, top_volume_pct=0.0,
                 blacklist=(), workers=1, symbol_lock=False, tiers=None, tier_cand_k=5,
                 shock_brake=None, log=print):
    """timeframe: 选币因子所用 K 线周期（换仓周期粒度，默认 1h）。
    top_volume_pct: >0 → PIT 票池相对口径（逐 rt 取 24h 量前 ceil(pct×N)，与地板可叠加，
    shock 篮子同步同口径；spec 2026-07-14-universe-top-volume-pct）；0=停用（基线可比）。
    sim_timeframe: 持仓成交仿真所用 K 线周期（None=沿用 timeframe）。传 '1m' 可解耦——
    选币仍在 1h 上（因子行为不变），持仓在 1m 上跑高保真触网/成交。
    workers: 网格仿真并行进程数（>1 用 ProcessPoolExecutor；结果与串行逐位一致）。
    symbol_lock: True=套用实盘 SymbolLockGate 同口径过滤（同币 period 锁窗内重选剔除，
    默认 False 保历史基线可比）。
    tiers: TierPolicy 三档评估（spec 2026-07-06-tiered-*）——tier0 并入 blacklist
    票池级剔除、top-K(tier_cand_k) 候选 + allocate_with_tiers 递补；与 symbol_lock
    互斥（两套口径不叠加）；None=现状零变化。
    shock_brake: None=关（默认,历史基线可比）;(k_hours, thr, pause_hours) 开启——
    与实盘 MarketShockBrake（spec 2026-07-08）同语义:blocked rt 的候选整轮剔除
    （只影响开格,信号数学与实盘同源,守卫测试见 test_shock_replay）。对齐实盘现配置
    传 (4, 0.04, 2)。
    扫参请直接用 build_grid_tasks（一次）+ simulate_tasks（多次），避免重复选币。"""
    if tiers is not None and symbol_lock:
        raise ValueError('tiers 与 symbol_lock 互斥（两套口径不叠加）')
    lev = leverage if leverage is not None else strategy_config['leverage']
    shock_blocked = None
    if shock_brake is not None:
        from gridtrade.backtest.shock_replay import blocked_rts
        k_h, thr, pause = shock_brake
        shock_blocked = blocked_rts(cache, universe, window_start, window_end, timeframe,
                                    k_h, thr, pause, min_quote_volume=min_quote_volume,
                                    top_volume_pct=top_volume_pct)
        log('[BT] shock_brake(k=%sh thr=%s X=%sh): blocked %d/%d rts'
            % (k_h, thr, pause, len(shock_blocked),
               len(pd.date_range(window_start, window_end, freq='1H'))))
    if tiers is not None:
        from gridtrade.core.tier_policy import effective_blacklist
        picks = select_grids(cache, universe, window_start, window_end, strategy_config,
                             factors, timeframe=timeframe,
                             min_quote_volume=min_quote_volume,
                             top_volume_pct=top_volume_pct,
                             blacklist=effective_blacklist(blacklist, tiers),
                             workers=workers, candidates_per_rt=int(tier_cand_k), log=log)
        if shock_blocked is not None:      # 刹车:blocked rt 整轮剔除(该轮空过,不递补,同实盘)
            picks = [p for p in picks if p[0] not in shock_blocked]
        picks, stats = allocate_with_tiers(picks, tiers,
                                           period=strategy_config['period'])
        log('[BT] tiers: rejected t1=%d t2=%d fallback=%s empty=%d'
            % (stats['rejected_tier1'], stats['rejected_tier2'],
               stats['fallback_hist'], stats['empty_rounds']))
        tasks = assemble_grid_tasks(cache, picks, strategy_config,
                                    sim_timeframe=sim_timeframe, timeframe=timeframe,
                                    log=log)
        return simulate_tasks(tasks, leverage=lev, fee_rate=fee_rate, taker_rate=taker_rate,
                              max_rate=max_rate,
                              stop_cfg=strategy_config['stop_loss_config'],
                              active_stop_mode=strategy_config.get('active_stop_mode', 'pv'),
                              pv_cfg=strategy_config.get('pv_config', {}), workers=workers)
    tasks = build_grid_tasks(cache, universe, window_start, window_end, strategy_config,
                             factors, timeframe=timeframe,
                             sim_timeframe=sim_timeframe, min_quote_volume=min_quote_volume,
                             top_volume_pct=top_volume_pct,
                             blacklist=blacklist, workers=workers, log=log)
    if shock_blocked is not None:          # 刹车:blocked rt 的任务剔除(只影响开格)
        tasks = [t for t in tasks if t[0] not in shock_blocked]
    if symbol_lock:
        tasks, n_rej = filter_tasks_symbol_lock(tasks, period=strategy_config['period'])
        log('[BT] symbol_lock: rejected %d tasks (每币≤1，与实盘 SymbolLockGate 同口径)' % n_rej)
    return simulate_tasks(tasks, leverage=lev, fee_rate=fee_rate, taker_rate=taker_rate,
                          max_rate=max_rate,
                          stop_cfg=strategy_config['stop_loss_config'],
                          active_stop_mode=strategy_config.get('active_stop_mode', 'pv'),
                          pv_cfg=strategy_config.get('pv_config', {}), workers=workers)


def _binance_datasource_1h(cache):
    """构造带退避的币安公共适配器 + 1h DataSource（网络；惰性导入；无需 API key）。"""
    import time
    import ccxt
    from gridtrade.backtest.datasource import DataSource
    from gridtrade.exchanges.binance import BinanceAdapter

    class _RetryBinance(BinanceAdapter):
        """对间歇 5xx/网络错误指数退避（预热用；不污染 core/live）。"""
        def _retry(self, fn, *a, **k):
            last = None
            for i in range(12):
                try:
                    return fn(*a, **k)
                except (ccxt.ExchangeNotAvailable, ccxt.NetworkError,
                        ccxt.RequestTimeout) as e:
                    last = e
                    time.sleep(min(2.0 * (i + 1), 8.0))
            raise last

        def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
            return self._retry(super().fetch_ohlcv, symbol, timeframe,
                               start_ms, end_ms)

        def fetch_funding_history(self, symbol, start_ms, end_ms):
            return self._retry(super().fetch_funding_history, symbol,
                               start_ms, end_ms)

    adapter = _RetryBinance(ccxt.binanceusdm({'enableRateLimit': True,
                                              'timeout': 30000}))
    return adapter, DataSource(adapter, cache, timeframe='1h')


def exclude_non_coin(symbols, adapter):
    """从 canonical 符号集剔除当前 exchangeInfo 的非 COIN 标的(TradFi 代币化永续),与实盘
    _include_market 共用同一 is_coin_market 谓词(单一事实源,spec 2026-07-15 §4.3)。
    保留退市 COIN:退市币不在当前 markets → 不在 non_coin → 不被剔(无幸存者偏差)。
    markets 未加载则 load(幂等;ccxt 缓存,紧随 prewarm 复用,全程一次 exchangeInfo)。
    fail-loud:load_markets 降级返回空(未抛异常)不得静默 fail-open 为"保留全量归档"
    (等于放行 TradFi)——此为回测唯一可能与实盘 fail-closed 背离之处,宁可整跑失败。
    返回 (kept: sorted list[str], removed: int)。"""
    adapter.client.load_markets()
    markets = adapter.client.markets
    if not markets:
        raise RuntimeError('load_markets 返回空 markets,无法做 COIN-only 过滤;'
                           '拒绝 fail-open 到含 TradFi 的旧票池')
    non_coin = {adapter.to_canonical(m['symbol']) for m in markets.values()
                if m.get('swap') and m.get('settle') == adapter.quote_currency
                and not is_coin_market(m)}
    kept = sorted(s for s in symbols if s not in non_coin)
    return kept, len(set(symbols) & non_coin)


def prewarm_1h(cache, universe, warm_start_ms, end_ms, *, log=print):
    """phase1：全市场 1h 选币 OHLCV——Vision 归档批量 + API 尾补(归档滞后1-2天)。
    返回 adapter（复用于 phase2）。"""
    from gridtrade.backtest import prewarm as PW
    from gridtrade.backtest import vision as V
    adapter, ds_1h = _binance_datasource_1h(cache)
    st = V.warm_vision(cache, universe, warm_start_ms, end_ms,
                       timeframes=('1h',), log=log)
    log('[prewarm] 1h@Vision(全市场 %d): %s' % (len(universe), st))
    log('[prewarm] 1h 尾补@API: %s'
        % PW.prewarm_ohlcv(ds_1h, universe, warm_start_ms, end_ms))
    return adapter


def prewarm_sim_and_funding(cache, adapter, selected, win_start_ms, end_ms, *,
                            sim_timeframe='1m', log=print):
    """phase2：仅选中币 持仓K线(Vision+API 尾补) + funding(Vision+API 尾补)。
    funding 月度归档无日度文件，当月尾部天然由 API 补（spec §6.2）。"""
    from gridtrade.backtest import prewarm as PW
    from gridtrade.backtest import vision as V
    from gridtrade.backtest.datasource import DataSource
    sim_tf = sim_timeframe or '1h'
    if sim_tf != '1h':
        st = V.warm_vision(cache, selected, win_start_ms, end_ms,
                           timeframes=(sim_tf,), log=log)
        log('[prewarm] %s@Vision(选中 %d): %s' % (sim_tf, len(selected), st))
        ds = DataSource(adapter, cache, timeframe=sim_tf)
        api = PW.prewarm_ohlcv(ds, selected, win_start_ms, end_ms)
        log('[prewarm] %s 尾补@API: %s' % (sim_tf, api))
        if selected and st[sim_tf]['rows'] == 0 and st['skipped_cached'] == 0 \
                and api['rows'] == 0:
            raise RuntimeError('%s 数据完全缺失——检查网络/币种/窗口 (retry_later=%d)'
                               % (sim_tf, st['retry_later']))
    fst = V.warm_vision(cache, selected, win_start_ms, end_ms,
                        timeframes=('funding',), log=log)
    log('[prewarm] funding@Vision(选中 %d): %s' % (len(selected), fst))
    ds_1h = DataSource(adapter, cache, timeframe='1h')
    log('[prewarm] funding 尾补@API: %s'
        % PW.prewarm_funding(ds_1h, selected, win_start_ms, end_ms))


_WARMUP_DAYS = 14   # 选币 1h 暖机（窗口前多取的天数）


def _resolve_window(argv):
    """解析回测窗口。**推荐绝对日期**（可复现、基线可比）：
      <start> <end> [sim_tf]     如 2026-03-01 2026-06-30 1m（end 含当天）
    也兼容相对天数：
      <days> [sim_tf]            如 90 1m（end=今日00:00 UTC）
    返回 (win_start, win_end, sim_tf, ftag)：Timestamp/Timestamp/str/文件名标签。"""
    if argv and '-' in str(argv[0]):                 # 绝对日期模式
        start = pd.Timestamp(argv[0]).normalize()
        end = pd.Timestamp(argv[1]).normalize() + pd.Timedelta(days=1)   # 含 end 当天
        sim_tf = argv[2] if len(argv) > 2 else '1m'
        ftag = '%s_%s' % (argv[0], argv[1])
    else:                                            # 相对天数模式（回退）
        days = int(argv[0]) if argv else 90
        sim_tf = argv[1] if len(argv) > 1 else '1m'
        end = pd.Timestamp.utcnow().normalize().tz_localize(None)   # 今日00:00 UTC(tz-naive,与cache同口径)
        start = end - pd.Timedelta(days=days)
        ftag = '%dd' % days
    return start, end, sim_tf, ftag


def main(argv=None):
    """CLI 单一入口：预热(如需自动下载/复用) + 回测。
    用法：
      python -m gridtrade.backtest.backtest_run 2026-03-01 2026-06-30 [1m]   # 绝对日期(推荐)
      python -m gridtrade.backtest.backtest_run 90 [1m]                       # 相对天数"""
    import sys
    import time

    argv = sys.argv[1:] if argv is None else argv
    win_start, win_end, sim_tf, ftag = _resolve_window(argv)
    warm_start = win_start - pd.Timedelta(days=_WARMUP_DAYS)

    def _ms(ts):
        return int(ts.value // 1_000_000)

    from gridtrade.backtest import vision as V
    root = V.default_cache_root()
    cache = ParquetCache(root)
    workers = safe_workers(os.environ.get('BT_WORKERS', '1'))  # 夹 ≤半数核心，防超订假死
    print('[BT] window %s -> %s | sim_tf=%s' % (win_start, win_end, sim_tf))

    t0 = time.time()
    # phase1: 解析全市场票池(−黑名单) + 预热全市场 1h
    _adapter, _ds1h = _binance_datasource_1h(cache)
    tiers = _tiers_from_env()
    if tiers is not None and os.environ.get('BT_SYMBOL_LOCK', '').lower() in ('1', 'true', 'on'):
        raise SystemExit('BT_TIER* 与 BT_SYMBOL_LOCK 互斥（两套口径不叠加）')
    bt_blacklist = BT_BLACKLIST
    if tiers is not None:
        from gridtrade.core.tier_policy import effective_blacklist
        bt_blacklist = effective_blacklist(BT_BLACKLIST, tiers)
        print('[BT] tiers 启用: tier0=%d tier1=%d cap=%d' %
              (len(tiers.tier0), len(tiers.tier1), tiers.tier2_cap))
    # 票池=归档全量合约（含退市，无幸存者偏差，spec §6.1）−黑名单 −非 COIN(TradFi,spec 2026-07-15)
    _arch = set(V.list_archive_symbols()) - set(bt_blacklist)
    universe, _n_tradfi = exclude_non_coin(_arch, _adapter)
    print('[BT] 全市场票池 %d 币(归档含退市,−黑名单 %d,−非COIN %d)'
          % (len(universe), len(bt_blacklist), _n_tradfi))
    st1h = V.warm_vision(cache, universe, _ms(warm_start), _ms(win_end),
                         timeframes=('1h',))
    print('[BT] 1h 预热@Vision: %s' % st1h)
    from gridtrade.backtest import prewarm as PW
    print('[BT] 1h 尾补@API: %s'
          % PW.prewarm_ohlcv(_ds1h, universe, _ms(warm_start), _ms(win_end)))

    # 选币(1h + PIT 前 55% 相对口径(可叠加地板) + 黑名单)——一次；权重/方向可经
    # BT_WEIGHTS/BT_SGCZ_DESC 覆盖
    _cand_k = int(os.environ.get('BT_TIER_CAND_K', 5)) if tiers is not None else 1
    _sc, _fac = _weights_from_env(BT_STRATEGY, BT_FACTORS)
    if _sc is not BT_STRATEGY or _fac is not BT_FACTORS:
        print('[BT] 权重覆盖: weights=%s Sgcz_asc=%s'
              % (_sc['weight_list'], _fac.get('Sgcz_5')))
    grids = select_grids(cache, universe, win_start, win_end, _sc, _fac,
                         timeframe='1h', min_quote_volume=BT_MIN_QUOTE_VOLUME_24H,
                         top_volume_pct=BT_UNIVERSE_TOP_PCT,
                         blacklist=bt_blacklist, workers=workers,
                         candidates_per_rt=_cand_k)
    if tiers is not None:
        grids, _ts = allocate_with_tiers(grids, tiers, period=BT_STRATEGY['period'])
        print('[BT] tiers: rejected t1=%d t2=%d fallback=%s empty=%d'
              % (_ts['rejected_tier1'], _ts['rejected_tier2'],
                 _ts['fallback_hist'], _ts['empty_rounds']))
    selected = sorted({row['symbol'] for _, _, row in grids})
    print('[BT] 选中 %d 币' % len(selected))

    # phase2: 仅选中币预热 1m/funding
    prewarm_sim_and_funding(cache, _adapter, selected, _ms(win_start), _ms(win_end),
                            sim_timeframe=sim_tf)
    print('[BT] prewarm done %.1fs' % (time.time() - t0))

    t0 = time.time()
    tasks = assemble_grid_tasks(cache, grids, BT_STRATEGY,
                                sim_timeframe=(None if sim_tf == '1h' else sim_tf), timeframe='1h')
    if os.environ.get('BT_SYMBOL_LOCK', '').lower() in ('1', 'true', 'on'):
        tasks, n_rej = filter_tasks_symbol_lock(tasks, period=BT_STRATEGY['period'])
        print('[BT] symbol_lock: rejected %d tasks (每币≤1，与实盘 SymbolLockGate 同口径)' % n_rej)
    df = simulate_tasks(tasks, leverage=BT_STRATEGY['leverage'],
                        stop_cfg=BT_STRATEGY['stop_loss_config'],
                        active_stop_mode=BT_STRATEGY.get('active_stop_mode', 'pv'),
                        pv_cfg=BT_STRATEGY.get('pv_config', {}), workers=workers)
    print('[BT] backtest %.1fs (workers=%d)' % (time.time() - t0, workers))

    tag = '@Vision+funding' if sim_tf == '1m' else ''
    print('\n===== 回测汇总（选币1h + 持仓%s%s）=====' % (sim_tf, tag))
    for k, v in summarize(df).items():
        print('  %s: %s' % (k, v))
    if not df.empty:
        out = os.path.join(root, '..', 'bt_%s_%s_grids.csv' % (ftag, sim_tf))
        df.to_csv(out, index=False)
        print('  funding_missing: %.3f | avg n_fills: %.2f'
              % (df['funding_missing'].mean(), df['n_fills'].mean()))
        print('[BT] 明细:', os.path.abspath(out), '| 共', len(df), '网格')


if __name__ == '__main__':
    main()
