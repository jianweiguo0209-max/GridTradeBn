"""端到端回测：选币回放 → 布网 → 持仓 bars → simulate_grid_engine → 聚合。
全部复用 gridtrade.core 纯函数；数据从 ParquetCache 读（预热后离线）。

本模块是**唯一预热+回测入口**：
  - run_backtest(...)         纯离线核心（只读 cache，无网络）——测试/复用都走它。
  - prewarm_1h(...)           phase1：全市场(−黑名单) 1h 选币 OHLCV(HL API，含暖机)，幂等按天缓存复用。
  - prewarm_sim_and_funding() phase2：仅选中币 持仓(1m 走 Reservoir S3 / 其它走 HL API) + funding。
                              1m 用 Reservoir 需 AWS 凭证（requester-pays）。
  - main()                    CLI：全市场解析+1h预热 → select_grids(一次) → 仅选中币预热 → 回测 → summarize → CSV。
                              跑：TZ=Asia/Shanghai .venv/bin/python -m gridtrade.backtest.backtest_run [days] [sim_tf]
网络依赖（ccxt/adapter/reservoir）全部在 prewarm_1h/prewarm_sim_and_funding/main 内**惰性导入**，保持核心离线可测。
"""
import os

import pandas as pd

from gridtrade.backtest import selection_replay as SR
from gridtrade.backtest.cache import ParquetCache
from gridtrade.config import DEFAULT_STOP_CFG, DEFAULT_STRATEGY_CONFIG
from gridtrade.core.grid_engine import simulate_grid_engine
from gridtrade.core.grid_params import calc_grid_params_v1, calc_grid_params_v2

# HL 回测默认策略（镜像 gridtrade.config 已验证参数；stop_loss_config 补 funding 止损）
HL_STRATEGY = {**DEFAULT_STRATEGY_CONFIG,
               'stop_loss_config': dict(DEFAULT_STOP_CFG),
               'active_stop_mode': 'pv',   # 主动止损默认 pv（回测扫描最优）
               'pv_config': {'mult': DEFAULT_STOP_CFG['pv_mult'], 'pnl_thr': DEFAULT_STOP_CFG['pv_pnl_thr'],
                             'period': DEFAULT_STOP_CFG['pv_period'], 'n': DEFAULT_STOP_CFG['pv_n']}}
HL_FACTORS = dict(DEFAULT_STRATEGY_CONFIG['factors'])

# 回测票池口径对齐 prod：全市场动态 −黑名单 −逐 run_time PIT $1M 成交额地板。
# 票池在 main() 里由 resolve_universe 解析全市场（见 main）；此处只放阈值/黑名单常量（可 env 覆写）。
BT_MIN_QUOTE_VOLUME_24H = float(os.environ.get('BT_MIN_QUOTE_VOLUME_24H', '1000000'))
BT_BLACKLIST = tuple(s.strip() for s in os.environ.get('BT_BLACKLIST', '').split(',') if s.strip())

# 1h 选币数据源自动切换：HL API 1h 滚动 ~5000 根≈208 天；更早窗口自动改走 Reservoir 归档。
RESERVOIR_START = pd.Timestamp('2025-07-31')   # Reservoir 1s 归档起点（实测列桶）
_API_1H_MAX_DAYS = 200                          # API 滚动可达阈值（208 天留余量）


def _pick_1h_source(warm_start, now):
    """纯函数：暖机起点早于 API 滚动可达范围 → 'reservoir'，否则 'api'（现路径字节不变）。"""
    return 'reservoir' if warm_start < now - pd.Timedelta(days=_API_1H_MAX_DAYS) else 'api'


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


def _simulate_grid_task(payload):
    """单个网格仿真（顶层函数，可 pickle → 供进程池并行）。
    payload=(data_task, cfg)：data_task 是选币/数据（可跨参数组合复用），cfg 是仿真配置。
    funding_df 已在父进程按持仓窗预切片（等价、payload 小）。"""
    (rt, offset, sym, entry, gp, bars_df, funding_df), cfg = payload
    pv_cfg = cfg['pv_cfg']
    sim = simulate_grid_engine(bars_df, gp, cap=1000.0, leverage=cfg['lev'], fee=cfg['fee_rate'],
                               max_rate=cfg['max_rate'], min_amount=0.0, stop_cfg=cfg['stop_cfg'],
                               funding_df=funding_df, neutral_init=False,
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
                 *, timeframe='1h', min_quote_volume=0.0, blacklist=(), workers=1, log=print):
    """只跑选币回放（1h + PIT 地板 + 黑名单），返回 [(rt, offset, row)]。offline。
    结果按选币参数 + 每币缓存天范围数据指纹磁盘缓存（BT_SELECT_CACHE=off 旁路）。"""
    from gridtrade.backtest import select_cache as SC
    use_cache = SC.enabled()
    key = params = None
    if use_cache:
        key, params = SC.compute_key(cache, universe, window_start, window_end, timeframe,
                                     min_quote_volume, blacklist, strategy_config, factors)
        hit = SC.load(cache, key, params)
        if hit is not None:
            log('[BT] select cache HIT %s (picks=%d)' % (key, len(hit)))
            return hit
    grids = []
    run_times = [pd.Timestamp(t) for t in pd.date_range(window_start, window_end, freq='1H')]
    SR.replay_selection(cache, universe, run_times, strategy_config, factors,
                        lambda rt, off, row: grids.append((rt, off, row.copy())),
                        timeframe=timeframe, min_quote_volume=min_quote_volume,
                        blacklist=blacklist, workers=workers, log=log)
    log('[BT] picks=%d' % len(grids))
    if use_cache:
        SC.save(cache, key, params, grids)
        log('[BT] select cache MISS %s (saved)' % key)
    return grids


def assemble_grid_tasks(cache, grids, strategy_config, *, sim_timeframe=None,
                        timeframe='1h', log=print):
    """由选中 grids 组装每格 data_task（载选中币 sim 序列 + holding_bars + funding 切片）。offline。"""
    sim_tf = sim_timeframe or timeframe
    period = strategy_config['period']
    price_limit = strategy_config['price_limit']
    stop_limit = strategy_config['stop_limit']
    grid_version = strategy_config.get('grid_version', 1)
    v2cfg = strategy_config.get('grid_v2_config', {})
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
        data_tasks.append((rt, int(offset), sym, float(row['close']), gp, bars_df, fd))
    return data_tasks


def build_grid_tasks(cache, universe, window_start, window_end, strategy_config, factors,
                     *, timeframe='1h', sim_timeframe=None, min_quote_volume=0.0,
                     blacklist=(), workers=1, log=print):
    """选币 + 组装（offline 便捷组合，run_backtest/测试用）。两段式预热见 main()。"""
    grids = select_grids(cache, universe, window_start, window_end, strategy_config, factors,
                         timeframe=timeframe, min_quote_volume=min_quote_volume,
                         blacklist=blacklist, workers=workers, log=log)
    return assemble_grid_tasks(cache, grids, strategy_config,
                               sim_timeframe=sim_timeframe, timeframe=timeframe, log=log)


def simulate_tasks(data_tasks, *, leverage, fee_rate=0.0005, max_rate=0.5, stop_cfg=None,
                   active_stop_mode='pv', pv_cfg=None, workers=1):
    """对已组装的 data_tasks 跑仿真（可并行）→ 明细 DataFrame。仿真配置在此传入，故同一批
    data_tasks 可反复用不同 (active_stop_mode/pv_cfg/stop_cfg) 仿真——扫参提速的关键。"""
    cfg = {'lev': leverage, 'fee_rate': fee_rate, 'max_rate': max_rate, 'stop_cfg': stop_cfg,
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


def run_backtest(cache, universe, window_start, window_end, strategy_config, factors,
                 *, timeframe='1h', sim_timeframe=None, fee_rate=0.0005,
                 max_rate=0.5, leverage=None, min_quote_volume=0.0, blacklist=(),
                 workers=1, symbol_lock=False, log=print):
    """timeframe: 选币因子所用 K 线周期（换仓周期粒度，默认 1h）。
    sim_timeframe: 持仓成交仿真所用 K 线周期（None=沿用 timeframe）。传 '1m' 可解耦——
    选币仍在 1h 上（因子行为不变），持仓在 1m 上跑高保真触网/成交。
    workers: 网格仿真并行进程数（>1 用 ProcessPoolExecutor；结果与串行逐位一致）。
    symbol_lock: True=套用实盘 SymbolLockGate 同口径过滤（同币 period 锁窗内重选剔除，
    默认 False 保历史基线可比）。
    扫参请直接用 build_grid_tasks（一次）+ simulate_tasks（多次），避免重复选币。"""
    lev = leverage if leverage is not None else strategy_config['leverage']
    tasks = build_grid_tasks(cache, universe, window_start, window_end, strategy_config,
                             factors, timeframe=timeframe,
                             sim_timeframe=sim_timeframe, min_quote_volume=min_quote_volume,
                             blacklist=blacklist, workers=workers, log=log)
    if symbol_lock:
        tasks, n_rej = filter_tasks_symbol_lock(tasks, period=strategy_config['period'])
        log('[BT] symbol_lock: rejected %d tasks (每币≤1，与实盘 SymbolLockGate 同口径)' % n_rej)
    return simulate_tasks(tasks, leverage=lev, fee_rate=fee_rate, max_rate=max_rate,
                          stop_cfg=strategy_config['stop_loss_config'],
                          active_stop_mode=strategy_config.get('active_stop_mode', 'pv'),
                          pv_cfg=strategy_config.get('pv_config', {}), workers=workers)


def _hl_datasource_1h(cache):
    """构造带退避的 HL 适配器 + 1h DataSource（网络；惰性导入）。返回 (adapter, ds_1h)。"""
    import time
    import ccxt
    from gridtrade.backtest.datasource import DataSource
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter

    class _RetryHL(HyperliquidAdapter):
        """对 HL /info 间歇 5xx/网络错误做指数退避（预热用；不污染 core/live）。"""
        def _retry(self, fn, *a, **k):
            last = None
            for i in range(12):
                try:
                    return fn(*a, **k)
                except (ccxt.ExchangeNotAvailable, ccxt.NetworkError, ccxt.RequestTimeout) as e:
                    last = e
                    time.sleep(min(2.0 * (i + 1), 8.0))
            raise last

        def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
            return self._retry(super().fetch_ohlcv, symbol, timeframe, start_ms, end_ms)

        def fetch_funding_history(self, symbol, start_ms, end_ms):
            return self._retry(super().fetch_funding_history, symbol, start_ms, end_ms)

    adapter = _RetryHL(ccxt.hyperliquid({'enableRateLimit': True, 'timeout': 30000}))
    return adapter, DataSource(adapter, cache, timeframe='1h')


def prewarm_1h(cache, universe, warm_start_ms, end_ms, *, log=print):
    """phase1：全市场 1h 选币 OHLCV(含暖机)。返回 adapter（复用于 phase2）。"""
    from gridtrade.backtest import prewarm as PW
    adapter, ds_1h = _hl_datasource_1h(cache)
    log('[prewarm] 1h 选币(全市场 %d): %s'
        % (len(universe), PW.prewarm_ohlcv(ds_1h, universe, warm_start_ms, end_ms)))
    return adapter


def prewarm_sim_and_funding(cache, adapter, selected, win_start_ms, end_ms, *,
                            sim_timeframe='1m', log=print):
    """phase2：仅选中币的持仓 OHLCV(1m 走 Reservoir / 其它走 HL) + funding。
    sim_timeframe=='1m' 时 Reservoir 是 requester-pays，需已配 AWS 凭证。网络依赖惰性导入。"""
    from gridtrade.backtest import prewarm as PW
    from gridtrade.backtest.datasource import DataSource
    from gridtrade.backtest.reservoir import warm_reservoir_1m
    sim_tf = sim_timeframe or '1h'
    if sim_tf == '1m':
        sr = warm_reservoir_1m(cache, selected, win_start_ms, end_ms, log=log)
        log('[prewarm] 1m@Reservoir(选中 %d): %s' % (len(selected), sr))
        if sr['rows'] == 0 and sr['skipped_cached'] == 0:
            raise RuntimeError('Reservoir 未拉到任何 1m 数据——检查 AWS 凭证/桶权限/币种 '
                               '(retry_later=%d)' % sr['retry_later'])
    elif sim_tf != '1h':
        ds = DataSource(adapter, cache, timeframe=sim_tf)
        log('[prewarm] %s 持仓(选中 %d): %s'
            % (sim_tf, len(selected), PW.prewarm_ohlcv(ds, selected, win_start_ms, end_ms)))
    ds_1h = DataSource(adapter, cache, timeframe='1h')
    log('[prewarm] funding(选中 %d): %s'
        % (len(selected), PW.prewarm_funding(ds_1h, selected, win_start_ms, end_ms)))


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

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'hl_validate')
    cache = ParquetCache(root)
    workers = int(os.environ.get('BT_WORKERS', '1'))
    print('[BT] window %s -> %s | sim_tf=%s' % (win_start, win_end, sim_tf))

    from gridtrade.backtest.prewarm import resolve_universe

    t0 = time.time()
    # 1h 数据源按窗口自动切换（单 run 单源无拼缝）；守卫先于任何网络调用
    source = _pick_1h_source(warm_start, pd.Timestamp.utcnow().tz_localize(None))
    print('[BT] 1h 数据源: %s' % source)
    if source == 'reservoir' and warm_start < RESERVOIR_START:
        raise SystemExit('[BT] 窗口过早：Reservoir 归档起点 %s，含 %d 天暖机最早窗口起点 %s'
                         % (RESERVOIR_START.date(), _WARMUP_DAYS,
                            (RESERVOIR_START + pd.Timedelta(days=_WARMUP_DAYS)).date()))
    # phase1: 解析全市场票池(−黑名单) + 预热全市场 1h
    _adapter, _ds1h = _hl_datasource_1h(cache)
    universe = resolve_universe(_ds1h, blacklist=BT_BLACKLIST)
    print('[BT] 全市场票池 %d 币(−黑名单 %d)' % (len(universe), len(BT_BLACKLIST)))
    if source == 'reservoir':
        from gridtrade.backtest import reservoir as RV
        print('[BT] 1h+1m 预热@Reservoir: %s'
              % RV.warm_reservoir_ohlcv(cache, universe, _ms(warm_start), _ms(win_end),
                                        timeframes=('1h', '1m')))
    else:
        from gridtrade.backtest import prewarm as PW
        print('[BT] 1h 预热: %s' % PW.prewarm_ohlcv(_ds1h, universe, _ms(warm_start), _ms(win_end)))

    # 选币(1h + PIT $1M 地板 + 黑名单)——一次
    grids = select_grids(cache, universe, win_start, win_end, HL_STRATEGY, HL_FACTORS,
                         timeframe='1h', min_quote_volume=BT_MIN_QUOTE_VOLUME_24H,
                         blacklist=BT_BLACKLIST, workers=workers)
    selected = sorted({row['symbol'] for _, _, row in grids})
    print('[BT] 选中 %d 币' % len(selected))

    # phase2: 仅选中币预热 1m/funding
    prewarm_sim_and_funding(cache, _adapter, selected, _ms(win_start), _ms(win_end),
                            sim_timeframe=sim_tf)
    print('[BT] prewarm done %.1fs' % (time.time() - t0))

    t0 = time.time()
    tasks = assemble_grid_tasks(cache, grids, HL_STRATEGY,
                                sim_timeframe=(None if sim_tf == '1h' else sim_tf), timeframe='1h')
    if os.environ.get('BT_SYMBOL_LOCK', '').lower() in ('1', 'true', 'on'):
        tasks, n_rej = filter_tasks_symbol_lock(tasks, period=HL_STRATEGY['period'])
        print('[BT] symbol_lock: rejected %d tasks (每币≤1，与实盘 SymbolLockGate 同口径)' % n_rej)
    df = simulate_tasks(tasks, leverage=HL_STRATEGY['leverage'],
                        stop_cfg=HL_STRATEGY['stop_loss_config'],
                        active_stop_mode=HL_STRATEGY.get('active_stop_mode', 'pv'),
                        pv_cfg=HL_STRATEGY.get('pv_config', {}), workers=workers)
    print('[BT] backtest %.1fs (workers=%d)' % (time.time() - t0, workers))

    tag = '@Reservoir+funding' if sim_tf == '1m' else ''
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
