"""
端到端回测驱动（支柱一/三：同源决策 + 单接缝注入）。

把已有组件串成能出 PnL 的回测：
  candidates(选币回放) → calc_grid_params(复用实盘布网) → 持仓期 bars(缓存)
  → simulate_grid(网格成交仿真) → apply_exit_rules(复用实盘 pnlRatio 止盈损) → 聚合

复用实盘纯函数保证 parity：
  - 选币：selection_replay（已验证 3/3）
  - 布网：account_0.utils.functions.calc_grid_params_v1/v2
  - 退出：grid_sim.apply_exit_rules（阈值取自 strategy_config.stop_loss_config）

⚠️ 仿真器未校准（见 USAGE §11）；用 1H bars 时网格内成交粒度粗。本驱动输出的 PnL 仅供
管线打通与相对比较，绝对值需先用 gridResult.csv 校准 + 接 1m 数据。

用法：
  TZ=Asia/Shanghai ../.venv/bin/python backtest_run.py --start "..." --end "..." \
      --cache-dir ../data/bt_verify/cache --manifest-dir ../data/bt_verify/manifest
"""
import argparse
import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_ACC = os.path.join(os.path.dirname(_HERE), 'account_0')
for _p in (_ACC, os.path.join(_ACC, 'utils'), os.path.join(_ACC, 'api')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bt_config as C  # noqa: E402
import prewarm  # noqa: E402
from cache import ParquetCache  # noqa: E402
import selection_replay as SR  # noqa: E402
from grid_sim import simulate_grid, apply_exit_rules  # noqa: E402
from utils.functions import calc_grid_params_v1, calc_grid_params_v2  # noqa: E402  实盘布网函数


def holding_bars(series_df, run_time, period, utc_offset):
    """取持仓周期 [run_time, run_time+period) 的 bars（按 UTC+offset 墙钟对齐）。"""
    td = pd.to_timedelta(period)
    local_t = series_df['candle_begin_time'] + pd.Timedelta(hours=utc_offset)
    sub = series_df[(local_t >= run_time) & (local_t < run_time + td)]
    return sub.sort_values('candle_begin_time')


def load_1m_holding(cache, symbol, run_time, period, utc_offset, proxies):
    """按需拉取并缓存某网格持仓期的 1m bars（namespace='1m'，per-day 幂等），返回持仓期切片。"""
    lo = run_time - pd.Timedelta(days=1)
    hi = run_time + pd.to_timedelta(period) + pd.Timedelta(days=1)
    prewarm._fetch_symbol_candles(cache, symbol, lo, hi, '1m', proxies)  # 幂等：已缓存的天跳过
    df = cache.read_all_days('1m', symbol)
    if df is None or df.empty:
        return pd.DataFrame()
    return holding_bars(df, run_time, period, utc_offset)


def summarize(df):
    """聚合：按 offset 复利，组合等权平均。pnl_ratio 是网格 margin 回报，offset 间等权。"""
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


def run_backtest(cache, universe, window_start, window_end, strategy_config,
                 factors, utc_offset, fee_rate=0.0005, sim_bar='1H', proxies=None, log=print):
    period = strategy_config['period']
    price_limit = strategy_config['price_limit']
    stop_limit = strategy_config['stop_limit']
    leverage = strategy_config['leverage']
    grid_version = strategy_config.get('grid_version', 1)
    v2cfg = strategy_config.get('grid_v2_config', {})
    stop_cfg = strategy_config['stop_loss_config']
    calc_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1

    series = SR._load_full_series(cache, universe)

    # 1) 选币回放，收集 (run_time, offset, row)
    grids = []
    run_times = [pd.Timestamp(t) for t in pd.date_range(window_start, window_end, freq='1H')]
    SR.replay_selection(cache, universe, run_times, strategy_config, factors, utc_offset,
                        lambda rt, off, row: grids.append((rt, off, row.copy())), log=log)
    log('[BT] 选中网格 %d 个，开始逐格仿真...' % len(grids))

    # 2) 逐格：布网 → 持仓 bars → 仿真 → 退出
    results = []
    for rt, offset, row in grids:
        sym = row['symbol']
        if sym not in series:
            continue
        if sim_bar == '1m':
            bars_df = load_1m_holding(cache, sym, rt, period, utc_offset, proxies)
        else:
            bars_df = holding_bars(series[sym], rt, period, utc_offset)
        if len(bars_df) == 0:
            continue
        px = calc_fn(row=row, price_limit=price_limit, stop_limit=stop_limit, v2_config=v2cfg)
        gp = dict(min_px=px['low_price'], max_px=px['high_price'], grid_num=px['grid_count'],
                  run_type='2', sz=1.0, lever=leverage, entry_px=float(row['close']),
                  tp_px=px['stop_high_price'], sl_px=px['stop_low_price'])
        bars = bars_df[['open', 'high', 'low', 'close']].to_dict('records')
        sim = simulate_grid(gp, bars, fee_rate=fee_rate)

        # pnlRatio 类退出（复用实盘阈值）：取最早触发
        ei, ereason = apply_exit_rules(sim['pnl_ratio_series'], stop_cfg)
        if ei is not None:
            pnl_ratio = sim['pnl_ratio_series'][ei]
            exit_reason = ereason
        else:
            pnl_ratio = sim['pnl_ratio']
            exit_reason = sim['exit_reason']

        results.append({
            'run_time': rt, 'offset': int(offset), 'symbol': sym,
            'entry': float(row['close']), 'grid_num': int(px['grid_count']),
            'low': round(px['low_price'], 8), 'high': round(px['high_price'], 8),
            'hold_bars': int(len(bars)), 'n_fills': int(sim['n_fills']),
            'pnl_ratio': float(pnl_ratio), 'exit_reason': exit_reason,
            'terminated': bool(sim['terminated']),
        })
    return pd.DataFrame(results)


def main():
    ap = argparse.ArgumentParser(description='OKX 网格端到端回测（v1，未校准）')
    ap.add_argument('--start', default=C.WINDOW_START)
    ap.add_argument('--end', default=C.WINDOW_END)
    ap.add_argument('--cache-dir', default=C.CACHE_DIR)
    ap.add_argument('--manifest-dir', default=C.MANIFEST_DIR)
    ap.add_argument('--fee-rate', type=float, default=0.0005)
    ap.add_argument('--sim-bar', default='1H', choices=['1H', '1m'],
                    help='持仓期仿真用的 bar 粒度；1m 更细但按需拉取（公共端点）')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    cache = ParquetCache(args.cache_dir)
    strategy_config = prewarm._load_strategy_config()
    universe, _ = prewarm.stage_instruments(cache, C.PROXIES, log=print)

    print('=' * 60)
    print('回测窗口 [%s, %s] fee_rate=%s sim_bar=%s TZ=%s' %
          (args.start, args.end, args.fee_rate, args.sim_bar, os.environ.get('TZ', '<未设置>')))
    print('⚠️ 仿真器未校准 + 1H 粒度，PnL 绝对值仅供管线验证/相对比较')
    print('=' * 60)

    df = run_backtest(cache, universe, pd.Timestamp(args.start), pd.Timestamp(args.end),
                      strategy_config, C.FACTORS, C.UTC_OFFSET, fee_rate=args.fee_rate,
                      sim_bar=args.sim_bar, proxies=C.PROXIES)

    out = args.out or os.path.join(args.manifest_dir, 'backtest_grids.csv')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_csv(out, index=False)

    s = summarize(df)
    print('\n===== 回测汇总 =====')
    for k, v in s.items():
        print('  %s: %s' % (k, v))
    print('逐格结果 -> %s' % out)
    if not df.empty:
        print('\n样本（前 12 行）:')
        print(df.head(12).to_string(index=False))


if __name__ == '__main__':
    main()
