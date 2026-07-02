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
               'stop_loss_config': {**DEFAULT_STOP_CFG, 'fundingRate_stop_loss': 0.0015}}
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


def run_backtest(cache, universe, window_start, window_end, strategy_config, factors,
                 utc_offset, *, timeframe='1h', sim_timeframe=None, fee_rate=0.0005,
                 max_rate=0.5, leverage=None, log=print):
    """timeframe: 选币因子所用 K 线周期（换仓周期粒度，默认 1h）。
    sim_timeframe: 持仓成交仿真所用 K 线周期（None=沿用 timeframe）。传 '1m' 可解耦——
    选币仍在 1h 上（因子行为不变），持仓在 1m 上跑高保真触网/成交。"""
    sim_tf = sim_timeframe or timeframe
    period = strategy_config['period']
    price_limit = strategy_config['price_limit']
    stop_limit = strategy_config['stop_limit']
    lev = leverage if leverage is not None else strategy_config['leverage']
    grid_version = strategy_config.get('grid_version', 1)
    v2cfg = strategy_config.get('grid_v2_config', {})
    stop_cfg = strategy_config['stop_loss_config']
    calc_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1

    # 持仓仿真用 sim_tf（可为 1m）；选币在 replay_selection 内部按 timeframe（1h）取数。
    series = SR.load_full_series(cache, universe, sim_tf)
    grids = []
    run_times = [pd.Timestamp(t) for t in pd.date_range(window_start, window_end, freq='1H')]
    SR.replay_selection(cache, universe, run_times, strategy_config, factors, utc_offset,
                        lambda rt, off, row: grids.append((rt, off, row.copy())),
                        timeframe=timeframe, log=log)
    log('[BT] picks=%d' % len(grids))

    results = []
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
        funding_df = cache.read_all_days('funding', sym)
        sim = simulate_grid_engine(bars_df, gp, cap=1000.0, leverage=lev, fee=fee_rate,
                                   max_rate=max_rate, min_amount=0.0, stop_cfg=stop_cfg,
                                   funding_df=funding_df, neutral_init=False)
        results.append({
            'run_time': rt, 'offset': int(offset), 'symbol': sym,
            'entry': float(row['close']), 'grid_num': int(px['grid_count']),
            'low': round(px['low_price'], 8), 'high': round(px['high_price'], 8),
            'hold_bars': int(len(bars_df)), 'n_fills': int(sim['n_trades']),
            'pnl_ratio': float(sim['pnl_ratio']), 'exit_reason': sim['exit_reason'],
            'terminated': bool(sim['terminated']),
            'funding_missing': bool(_funding_missing(funding_df, bars_df)),
        })
    return pd.DataFrame(results)


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


def main(argv=None):
    """CLI 单一入口：预热(如需自动下载/复用) + 回测。
    用法：python -m gridtrade.backtest.backtest_run [days=90] [sim_tf=1m]"""
    import sys
    import time

    argv = sys.argv[1:] if argv is None else argv
    days = int(argv[0]) if len(argv) > 0 else 90
    sim_tf = argv[1] if len(argv) > 1 else '1m'

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'hl_validate')
    cache = ParquetCache(root)
    one_h = 3600_000
    end_ms = int(time.time() * 1000)
    warm_start = end_ms - (days + 14) * 24 * one_h    # 选币 1h 暖机
    win_start = end_ms - days * 24 * one_h
    print('[BT] window %s -> %s | days=%d sim_tf=%s'
          % (pd.to_datetime(win_start, unit='ms'), pd.to_datetime(end_ms, unit='ms'), days, sim_tf))

    t0 = time.time()
    prewarm_all(cache, HL_UNIVERSE, warm_start, win_start, end_ms, sim_timeframe=sim_tf)
    print('[BT] prewarm done %.1fs' % (time.time() - t0))

    t0 = time.time()
    df = run_backtest(cache, HL_UNIVERSE, pd.to_datetime(win_start, unit='ms'),
                      pd.to_datetime(end_ms, unit='ms'), HL_STRATEGY, HL_FACTORS, utc_offset=0,
                      timeframe='1h', sim_timeframe=(None if sim_tf == '1h' else sim_tf))
    print('[BT] backtest %.1fs' % (time.time() - t0))

    tag = '@Reservoir+funding' if sim_tf == '1m' else ''
    print('\n===== 回测汇总（选币1h + 持仓%s%s）=====' % (sim_tf, tag))
    for k, v in summarize(df).items():
        print('  %s: %s' % (k, v))
    if not df.empty:
        out = os.path.join(root, '..', 'bt_%dd_%s_grids.csv' % (days, sim_tf))
        df.to_csv(out, index=False)
        print('  funding_missing: %.3f | avg n_fills: %.2f'
              % (df['funding_missing'].mean(), df['n_fills'].mean()))
        print('[BT] 明细:', os.path.abspath(out), '| 共', len(df), '网格')


if __name__ == '__main__':
    main()
