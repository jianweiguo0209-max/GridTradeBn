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

from gridtrade.backtest.backtest_run import (BT_STRATEGY, _FUNDING_BACK_MS,
                                             pv_spike_for_window, simulate_tasks)
from gridtrade.backtest.sweep import FEE_MAKER, FEE_TAKER, GEARING, MAX_RATE
from gridtrade.config import DEFAULT_STOP_CFG
from gridtrade.core.grid_engine import grid_order_info
from gridtrade.core.grid_params import calc_grid_params_v2
from gridtrade.core.selection import proceed_calc_symbol_factor

# 执行对账**直接复用正规回测入口**(pv_spike_for_window + simulate_tasks,与 sweep.run_arm 同一条路)
# ——一致性由构造保证,不靠平行实现眼睛比对(2026-07-19 用户指正:"只 recon_live 有效还不如不改")。
# stop/pv 参数单源=config DEFAULT_STOP_CFG(与 sweep.live_baseline 同源)。
_STOP = {'stop_loss': DEFAULT_STOP_CFG['stop_loss'],
         'trailing_k': DEFAULT_STOP_CFG['trailing_k'],
         'trailing_floor': DEFAULT_STOP_CFG['trailing_floor'],
         'fundingRate_stop_loss': DEFAULT_STOP_CFG['fundingRate_stop_loss'],
         'pv_pnl_thr': DEFAULT_STOP_CFG['pv_pnl_thr'], 'pv_mult': DEFAULT_STOP_CFG['pv_mult'],
         'pv_period': DEFAULT_STOP_CFG['pv_period'], 'pv_n': DEFAULT_STOP_CFG['pv_n']}
_PV_CFG = {'pnl_thr': _STOP['pv_pnl_thr'], 'mult': _STOP['pv_mult'],
           'n': _STOP['pv_n'], 'period': _STOP['pv_period']}


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


def _fetch_funding(client, sym, start, end):
    """拉资金费历史 → 列 ts/fundingRate(与缓存 funding 同形);取不到 → None(引擎按无 funding 跑)。"""
    try:
        rows = client.fetch_funding_rate_history(client.market(sym)['symbol'],
                                                 since=int(start.value // 1_000_000), limit=100)
        endms = int(end.value // 1_000_000)
        out = [{'ts': int(x['timestamp']), 'fundingRate': float(x['fundingRate'])}
               for x in (rows or []) if x.get('timestamp') and int(x['timestamp']) <= endms]
        return pd.DataFrame(out) if out else None
    except Exception:
        return None


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

    # ── 执行对账:**走正规回测入口**(与 sweep.run_arm 完全同一条路,一致性构造保证)──
    # data_task 形状 = assemble_grid_tasks 的 (rt, off, sym, entry, gp, bars, fd, pv_df);
    # pv 预算 = pv_spike_for_window(正规函数,窗前 (n+8)×15min 前置历史);funding 预切 lo−9h
    # (=_FUNDING_BACK_MS,与 assemble 同源常量);sizing/lot 截断由 simulate_tasks 内部处理。
    pre = pd.Timedelta(_STOP['pv_period']) * (_STOP['pv_n'] + 8)
    full = _fetch(client, sym, '1m', rt - pre, rt + pd.Timedelta('12H'))
    bars = full[full['candle_begin_time'] >= rt].reset_index(drop=True)
    pv_df = pv_spike_for_window(full, bars, _PV_CFG)
    fd = _fetch_funding(client, sym,
                        rt - pd.Timedelta(milliseconds=_FUNDING_BACK_MS),
                        rt + pd.Timedelta('12H'))
    gp = {'low_price': g['low_price'], 'high_price': g['high_price'],
          'grid_count': int(g['grid_count']), 'stop_low_price': g['stop_low_price'],
          'stop_high_price': g['stop_high_price']}
    task = (rt, off, sym, float(g['entry_price']), gp, bars, fd, pv_df)
    df = simulate_tasks([task], leverage=GEARING / MAX_RATE, fee_rate=FEE_MAKER,
                        taker_rate=FEE_TAKER, max_rate=MAX_RATE, stop_cfg=_STOP,
                        active_stop_mode='pv', pv_cfg=_PV_CFG, workers=1)
    r = df.iloc[0]
    print("  [执行] 实盘: %s  pnl_ratio=%+.6f" % (g.get('close_reason', '?'), g.get('pnl_ratio', 0)))
    print("  [执行] 回测: %s  pnl_ratio=%+.6f  n_fills=%d"
          % (r['exit_reason'], r['pnl_ratio'], int(r['n_fills'])))

    # ── 对齐时点估值（可选,g 带 closed_at 时;同样走 simulate_tasks,只是截窗+关退出）──
    # 隔离「时机分量」(回测逐 1m 判 vs 实盘 900s 节流,9199503 在案二阶残差)与「价格分量」
    # (bar close vs 市价 taker 点差)。引擎不做此对齐——会重新引入相位敏感(方案C 刚消灭)。
    if g.get('closed_at'):
        cl = pd.Timestamp(int(g['closed_at']), unit='ms').floor('min')   # 截到成交所在 bar
        clip = bars[bars['candle_begin_time'] <= cl].reset_index(drop=True)
        if len(clip):
            t2 = (rt, off, sym, float(g['entry_price']), gp, clip, fd, None)
            d2 = simulate_tasks([t2], leverage=GEARING / MAX_RATE, fee_rate=FEE_MAKER,
                                taker_rate=FEE_TAKER, max_rate=MAX_RATE, stop_cfg=None,
                                active_stop_mode='none', workers=1)
            r2 = d2.iloc[0]
            print("  [执行·对齐实盘平仓时点 %s] 回测 pnl_ratio=%+.6f (Δ vs 实盘 %+.4fpp)"
                  % (cl.strftime('%H:%M'), r2['pnl_ratio'],
                     (r2['pnl_ratio'] - g.get('pnl_ratio', 0)) * 100))


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
