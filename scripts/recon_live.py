"""实盘 vs 回测对账工具（2026-07-19 mainnet 对账固化，见 memory testnet-recon-and-replay-infra）。

三层对账：选币因子/建网几何/sizing（byte）→ 触网/成交 → pv/funding 退出归因。全用生产纯函数复现，
保口径一致。行情从币安公开 API 拉（无需 key）；实盘网格记录先用 dump_live_grids 查出（容器内）。

用法：
  # ① 容器内 dump 实盘已完成网格（offset 是 PG 保留字，脚本已加引号）：
  flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_live_grids.py > grids.json
  # ② 本地对账（读 grids.json，逐网格复现建网+执行，对比）：
  .venv/bin/python -m scripts.recon_live grids.json

grids.json 每条须含：symbol, offset, entry_price, low_price, high_price, stop_low_price,
stop_high_price, grid_count, order_num, cap, created_at(ms), close_reason, pnl_ratio。
"""
import json
import sys

import pandas as pd

from gridtrade.backtest.backtest_run import BT_STRATEGY
from gridtrade.core.grid_engine import grid_order_info, simulate_grid_engine
from gridtrade.core.grid_params import calc_grid_params_v2
from gridtrade.core.selection import proceed_calc_symbol_factor

_STOP = {'stop_loss': 0.045, 'trailing_k': 0.3, 'trailing_floor': 0.00618,
         'fundingRate_stop_loss': 0.0015, 'pv_pnl_thr': 0.005, 'pv_mult': 3,
         'pv_period': '15min', 'pv_n': 100}


def _client():
    import ccxt
    c = ccxt.binanceusdm({'enableRateLimit': True})
    c.load_markets()
    return c


def _fetch(client, sym, tf, start, end):
    """拉 [start, end] 的 K线（tf='1h'/'1m'），补 vol/volCcy/quote_volume（与 ccxt_adapter 同口径）。"""
    step = int(pd.Timedelta(tf).total_seconds() * 1000)
    rows, cur, endms = [], int(start.value // 1_000_000), int(end.value // 1_000_000)
    while cur < endms:
        b = client.fetch_ohlcv(client.market(sym)['symbol'], tf, since=cur, limit=1000)
        if not b:
            break
        rows += b
        cur = b[-1][0] + step
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'vol']).drop_duplicates('ts')
    df['candle_begin_time'] = pd.to_datetime(df['ts'], unit='ms')
    df = df[(df['candle_begin_time'] >= start) & (df['candle_begin_time'] <= end)]
    df['symbol'] = sym
    df['volCcy'] = df['vol']
    df['quote_volume'] = (df['open'] + df['close']) / 2.0 * df['vol']
    return df.reset_index(drop=True)


def _cmp(label, rep, live, rel=5e-3):
    d = abs(rep - live) / abs(live) * 100 if live else (0 if not rep else 100)
    return "%-8s 回测 %.10g  实盘 %.10g  %s" % (label, rep, live, '✓' if d < rel * 100 else '✗ 差%.3f%%' % d)


def recon_one(client, g):
    sym, off = g['symbol'], int(g['offset'])
    rt = pd.Timestamp(g['created_at'], unit='ms').floor('h')
    print("\n===== %s  offset=%d  开=%s =====" % (sym, off, rt))

    # ── 建网几何对账（1h 选币因子 → calc_grid_params_v2）──
    h1 = _fetch(client, sym, '1h', rt - pd.Timedelta(days=16), rt)
    fac = proceed_calc_symbol_factor({sym: h1}, rt, '12H', off)
    if fac is not None and not fac.empty:
        row = fac[fac['symbol'] == sym].iloc[-1].to_dict()
        gp = calc_grid_params_v2(row=row, price_limit=BT_STRATEGY['price_limit'],
                                 stop_limit=BT_STRATEGY['stop_limit'],
                                 v2_config=BT_STRATEGY['grid_v2_config'])
        print("  [建网] " + _cmp('low', gp['low_price'], g['low_price']))
        print("  [建网] " + _cmp('high', gp['high_price'], g['high_price']))
        print("  [建网] " + _cmp('count', gp['grid_count'], g['grid_count']))
    gi = grid_order_info(g['cap'], 3.4, g['low_price'], g['high_price'], int(g['grid_count']),
                         g['stop_low_price'], g['stop_high_price'], min_amount=0.0, max_rate=1.0)
    print("  [sizing] " + _cmp('order_num', gi['每笔数量'], g['order_num']))

    # ── 执行对账（1m 持仓窗 → simulate_grid_engine）──
    bars = _fetch(client, sym, '1m', rt, rt + pd.Timedelta('12H'))
    r = simulate_grid_engine(bars, {'low_price': g['low_price'], 'high_price': g['high_price'],
                                    'grid_count': int(g['grid_count']), 'stop_low_price': g['stop_low_price'],
                                    'stop_high_price': g['stop_high_price']},
                             cap=1000.0, leverage=3.4, max_rate=1.0, stop_cfg=_STOP,
                             neutral_init=False, active_stop_mode='pv', pv_pnl_thr=_STOP['pv_pnl_thr'],
                             pv_mult=_STOP['pv_mult'], pv_period=_STOP['pv_period'], pv_n=_STOP['pv_n'])
    print("  [执行] 实盘: %s  pnl_ratio=%+.6f" % (g.get('close_reason', '?'), g.get('pnl_ratio', 0)))
    print("  [执行] 回测: %s  pnl_ratio=%+.6f  n_fills=%d" % (r['exit_reason'], r['pnl_ratio'], r['n_trades']))


def main():
    if len(sys.argv) < 2:
        print(__doc__); return 1
    grids = json.load(open(sys.argv[1]))
    client = _client()
    for g in grids:
        recon_one(client, g)
    return 0


if __name__ == '__main__':
    sys.exit(main())
