"""端到端回测：选币回放 → 布网 → 持仓 bars → simulate_grid_engine → 聚合。
全部复用 gridtrade.core 纯函数；数据从 ParquetCache 读（预热后离线）。
"""
import pandas as pd

from gridtrade.backtest import selection_replay as SR
from gridtrade.core.grid_engine import simulate_grid_engine
from gridtrade.core.grid_params import calc_grid_params_v1, calc_grid_params_v2


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
                 utc_offset, *, timeframe='1h', fee_rate=0.0005, max_rate=0.5,
                 leverage=None, log=print):
    period = strategy_config['period']
    price_limit = strategy_config['price_limit']
    stop_limit = strategy_config['stop_limit']
    lev = leverage if leverage is not None else strategy_config['leverage']
    grid_version = strategy_config.get('grid_version', 1)
    v2cfg = strategy_config.get('grid_v2_config', {})
    stop_cfg = strategy_config['stop_loss_config']
    calc_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1

    series = SR.load_full_series(cache, universe, timeframe)
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
