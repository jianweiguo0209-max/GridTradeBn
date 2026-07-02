"""端到端回测：选币回放 → 布网 → 持仓 bars → simulate_grid_engine → 聚合。
全部复用 gridtrade.core 纯函数；数据从 ParquetCache 读（预热后离线）。

本模块是**唯一预热+回测入口**：
  - run_backtest(...)  纯离线核心（只读 cache，无网络）——测试/复用都走它。
  - prewarm_all(...)   单一预热：1h 选币(HL API) + 持仓(1m 走 Reservoir S3 / 其它走 HL API) + funding。
                       幂等，按天缓存复用；1m 用 Reservoir 需 AWS 凭证（requester-pays）。
  - main()             CLI：prewarm_all → run_backtest → summarize → CSV。
                       跑：TZ=Asia/Shanghai .venv/bin/python -m gridtrade.backtest.backtest_run [days] [sim_tf]
网络依赖（ccxt/adapter/reservoir）全部在 prewarm_all/main 内**惰性导入**，保持核心离线可测。
"""
import os

import pandas as pd

from gridtrade.backtest import selection_replay as SR
from gridtrade.backtest.cache import ParquetCache
from gridtrade.config import DEFAULT_STOP_CFG, DEFAULT_STRATEGY_CONFIG
from gridtrade.core.grid_engine import simulate_grid_engine
from gridtrade.core.grid_params import calc_grid_params_v1, calc_grid_params_v2

# HL 回测默认票池/策略（镜像 gridtrade.config 已验证参数；stop_loss_config 补 funding 止损）
HL_UNIVERSE = ['BTC/USDC:USDC', 'ETH/USDC:USDC', 'SOL/USDC:USDC', 'AVAX/USDC:USDC',
               'ARB/USDC:USDC', 'OP/USDC:USDC', 'LINK/USDC:USDC', 'DOGE/USDC:USDC']
HL_STRATEGY = {**DEFAULT_STRATEGY_CONFIG,
               'stop_loss_config': dict(DEFAULT_STOP_CFG),
               'active_stop_mode': 'pv',   # 主动止损默认 pv（回测扫描最优）
               'pv_config': {'mult': DEFAULT_STOP_CFG['pv_mult'], 'pnl_thr': DEFAULT_STOP_CFG['pv_pnl_thr'],
                             'period': DEFAULT_STOP_CFG['pv_period'], 'n': DEFAULT_STOP_CFG['pv_n']}}
HL_FACTORS = dict(DEFAULT_STRATEGY_CONFIG['factors'])


def holding_bars(series_df, run_time, period, utc_offset):
    td = pd.to_timedelta(period)
    local_t = series_df['candle_begin_time'] + pd.Timedelta(hours=utc_offset)
    sub = series_df[(local_t >= run_time) & (local_t < run_time + td)]
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
                               pv_period=pv_cfg.get('period', '15min'))
    return {
        'run_time': rt, 'offset': int(offset), 'symbol': sym,
        'entry': entry, 'grid_num': int(gp['grid_count']),
        'low': round(gp['low_price'], 8), 'high': round(gp['high_price'], 8),
        'hold_bars': int(len(bars_df)), 'n_fills': int(sim['n_trades']),
        'pnl_ratio': float(sim['pnl_ratio']), 'exit_reason': sim['exit_reason'],
        'terminated': bool(sim['terminated']),
        'funding_missing': bool(_funding_missing(funding_df, bars_df)),
    }


def build_grid_tasks(cache, universe, window_start, window_end, strategy_config, factors,
                     utc_offset, *, timeframe='1h', sim_timeframe=None, log=print):
    """选币回放 + 组装每格数据 payload（**不含仿真配置**，故可跨多组参数复用）。
    返回 data_task 列表：(rt, offset, sym, entry, gp, bars_df, funding_df)。
    选币是回测里最贵的一步——扫参时 build 一次、simulate_tasks 多次，可省 N-1 次选币。"""
    sim_tf = sim_timeframe or timeframe
    period = strategy_config['period']
    price_limit = strategy_config['price_limit']
    stop_limit = strategy_config['stop_limit']
    grid_version = strategy_config.get('grid_version', 1)
    v2cfg = strategy_config.get('grid_v2_config', {})
    calc_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1

    series = SR.load_full_series(cache, universe, sim_tf)
    grids = []
    run_times = [pd.Timestamp(t) for t in pd.date_range(window_start, window_end, freq='1H')]
    SR.replay_selection(cache, universe, run_times, strategy_config, factors, utc_offset,
                        lambda rt, off, row: grids.append((rt, off, row.copy())),
                        timeframe=timeframe, log=log)
    log('[BT] picks=%d' % len(grids))

    funding_by_sym = {}
    data_tasks = []
    for rt, offset, row in grids:
        sym = row['symbol']
        if sym not in series:
            continue
        bars_df = holding_bars(series[sym], rt, period, utc_offset)
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
    return pd.DataFrame(results)


def run_backtest(cache, universe, window_start, window_end, strategy_config, factors,
                 utc_offset, *, timeframe='1h', sim_timeframe=None, fee_rate=0.0005,
                 max_rate=0.5, leverage=None, workers=1, log=print):
    """timeframe: 选币因子所用 K 线周期（换仓周期粒度，默认 1h）。
    sim_timeframe: 持仓成交仿真所用 K 线周期（None=沿用 timeframe）。传 '1m' 可解耦——
    选币仍在 1h 上（因子行为不变），持仓在 1m 上跑高保真触网/成交。
    workers: 网格仿真并行进程数（>1 用 ProcessPoolExecutor；结果与串行逐位一致）。
    扫参请直接用 build_grid_tasks（一次）+ simulate_tasks（多次），避免重复选币。"""
    lev = leverage if leverage is not None else strategy_config['leverage']
    tasks = build_grid_tasks(cache, universe, window_start, window_end, strategy_config,
                             factors, utc_offset, timeframe=timeframe,
                             sim_timeframe=sim_timeframe, log=log)
    return simulate_tasks(tasks, leverage=lev, fee_rate=fee_rate, max_rate=max_rate,
                          stop_cfg=strategy_config['stop_loss_config'],
                          active_stop_mode=strategy_config.get('active_stop_mode', 'pv'),
                          pv_cfg=strategy_config.get('pv_config', {}), workers=workers)


def prewarm_all(cache, universe, warm_start_ms, win_start_ms, end_ms, *,
                sim_timeframe='1m', log=print):
    """单一预热（幂等，按天缓存复用）：
      - 1h 选币 OHLCV（HL API，从 warm_start 起含暖机）
      - 持仓 OHLCV：sim_timeframe=='1m' 走 Reservoir S3(download-or-reuse)，其它走 HL API
      - funding（HL API）
    sim_timeframe=='1m' 时 Reservoir 是 requester-pays，需已配 AWS 凭证。网络依赖惰性导入。"""
    import time

    import ccxt

    from gridtrade.backtest import prewarm as PW
    from gridtrade.backtest.datasource import DataSource
    from gridtrade.backtest.reservoir import warm_reservoir_1m
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
    ds_1h = DataSource(adapter, cache, timeframe='1h')

    log('[prewarm] 1h 选币: %s' % PW.prewarm_ohlcv(ds_1h, universe, warm_start_ms, end_ms))

    sim_tf = sim_timeframe or '1h'
    if sim_tf == '1m':
        sr = warm_reservoir_1m(cache, universe, win_start_ms, end_ms, log=log)
        log('[prewarm] 1m@Reservoir: %s' % sr)
        if sr['rows'] == 0 and sr['skipped_cached'] == 0:
            raise RuntimeError('Reservoir 未拉到任何 1m 数据——检查 AWS 凭证 / 桶权限 / 币种，'
                               '或窗口内数据在 S3 尚未发布（retry_later=%d）' % sr['retry_later'])
    elif sim_tf != '1h':
        ds = DataSource(adapter, cache, timeframe=sim_tf)
        log('[prewarm] %s 持仓: %s' % (sim_tf, PW.prewarm_ohlcv(ds, universe, win_start_ms, end_ms)))

    log('[prewarm] funding: %s' % PW.prewarm_funding(ds_1h, universe, win_start_ms, end_ms))


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
    print('[BT] window %s -> %s | sim_tf=%s' % (win_start, win_end, sim_tf))

    t0 = time.time()
    prewarm_all(cache, HL_UNIVERSE, _ms(warm_start), _ms(win_start), _ms(win_end), sim_timeframe=sim_tf)
    print('[BT] prewarm done %.1fs' % (time.time() - t0))

    workers = int(os.environ.get('BT_WORKERS', '1'))    # 并行进程数：BT_WORKERS=4 提速
    t0 = time.time()
    df = run_backtest(cache, HL_UNIVERSE, win_start, win_end, HL_STRATEGY, HL_FACTORS, utc_offset=0,
                      timeframe='1h', sim_timeframe=(None if sim_tf == '1h' else sim_tf),
                      workers=workers)
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
