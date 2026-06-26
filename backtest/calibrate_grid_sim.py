"""
网格仿真器校准：对真实平仓网格，用其实际参数 + 持仓期 1m 价格跑 simulate_grid，
对比 仿真 pnl_ratio vs OKX 真实 pnlRatio，量化偏差。

输入：data/order/gridResult_clean.csv（从 gridResult.csv 整理；open_bj=开仓时间北京时区）。
持仓窗口：开仓时间不含关仓时间 → 假设持仓 = open .. open+hold_hours（默认 12H，手动停止多为换仓平）。
仅对「未触发 TP/SL」的网格用纯 simulate_grid 终值对比（手动停止符合）。

用法：TZ=Asia/Shanghai ../.venv/bin/python calibrate_grid_sim.py [--hold-hours 12] [--fee-rate 0.0002]
"""
import argparse
import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import okx_history as H  # noqa: E402
from grid_sim import simulate_grid  # noqa: E402

CLEAN = os.path.join(os.path.dirname(_HERE), 'data', 'order', 'gridResult_clean.csv')


def _ms_utc_from_bj(bj_str):
    """开仓时间是北京(UTC+8)墙钟 → 转 UTC 毫秒（OKX K线为 UTC）。"""
    bj = pd.Timestamp(bj_str)
    return int((bj - pd.Timedelta(hours=8)).value // 1_000_000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hold-hours', type=float, default=12.0)
    ap.add_argument('--fee-rate', type=float, default=0.0002)  # OKX maker 量级
    ap.add_argument('--csv', default=CLEAN)
    args = ap.parse_args()

    grids = pd.read_csv(args.csv)
    print('校准样本 %d 条，假设持仓 %.1fH，fee_rate=%s' % (len(grids), args.hold_hours, args.fee_rate))
    print('=' * 100)
    rows = []
    for _, g in grids.iterrows():
        sym = g['symbol']
        open_ms = _ms_utc_from_bj(g['open_bj'])
        end_ms = open_ms + int(args.hold_hours * 3600 * 1000)
        df = H.fetch_candles_range(sym, open_ms, end_ms, bar='1m')
        if df is None or df.empty:
            print('%-20s 无 1m 数据，跳过' % sym); continue
        bars = df[['open', 'high', 'low', 'close']].to_dict('records')
        params = dict(min_px=float(g['minPx']), max_px=float(g['maxPx']), grid_num=int(g['gridNum']),
                      run_type='2', sz=float(g['sz']), lever=float(g['lever']),
                      entry_px=float(g['entry']), tp_px=float(g['tpPx']), sl_px=float(g['slPx']))
        sim = simulate_grid(params, bars, fee_rate=args.fee_rate)
        real = float(g['real_pnl_ratio'])
        simr = sim['pnl_ratio']
        rows.append({'symbol': sym, 'bars': len(bars), 'n_fills': sim['n_fills'],
                     'sim_terminated': sim['terminated'], 'sim_exit': sim['exit_reason'],
                     'real_pnl_ratio': real, 'sim_pnl_ratio': simr, 'diff': simr - real})
        print('%-20s bars=%4d fills=%3d | real=%+.4f%% sim=%+.4f%% diff=%+.4f%% %s'
              % (sym, len(bars), sim['n_fills'], real * 100, simr * 100, (simr - real) * 100,
                 ('[sim ' + str(sim['exit_reason']) + ']') if sim['terminated'] else ''))

    if rows:
        r = pd.DataFrame(rows)
        mae = r['diff'].abs().mean()
        print('=' * 100)
        print('MAE(|sim-real|) = %.4f%%  | 平均偏差 = %+.4f%%' % (mae * 100, r['diff'].mean() * 100))
        print('注：样本仅 %d 条且关仓时间缺失(假设%.0fH)，这是量级 sanity check，非严谨校准。'
              % (len(r), args.hold_hours))


if __name__ == '__main__':
    main()
