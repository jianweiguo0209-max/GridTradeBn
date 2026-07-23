"""P2 实盘反事实巡检(spec §4 Phase2):对已收盘的实盘选币轮重建票池、逐币双口径
反事实,出当日成绩单 + 选中币对账本 Δ(锚:pv 格复现,Δ中位≤0.5pp)。

用法:
  # ① 容器内 dump(两份):
  flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_selection_snapshots.py > snaps.json
  RECON_ALL=  flyctl ssh console -a gridtrade-bi-prod -C "python3" < scripts/dump_live_grids.py > grids.json
  # ② 本地巡检(公开 fapi 拉行情,本机 IP 权重与 prod 隔离,仍 250ms pace):
  .venv/bin/python scripts/cf_patrol.py snaps.json grids.json

票池重建=选币同规则(spec fallback:snapshots.ranked 只存选中):fapi exchangeInfo
USDT 本位永续 TRADING − 黑名单 → 1h klines → build_pit_candidates(top55%)。
Atr_5=proceed_calc_symbol_factor 生产因子路径(recon_live 同款)。
只处理 rt+12h 已收盘的轮。产物 data/score_research_2026-07-21/ablation/cf_live_<日期>.parquet。
"""
import contextlib
import importlib.util
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest.selection_replay import build_pit_candidates
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.selection import proceed_calc_symbol_factor
from gridtrade.core.tier_policy import effective_blacklist
from gridtrade.exchanges.base import CANDLE_COLS

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
API = 'https://fapi.binance.com/fapi/v1'
TOP_VOLUME_PCT = 0.55
_spec = importlib.util.spec_from_file_location('cf_eval', RD + '/cf_eval.py')
cf_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf_eval)
_spec2 = importlib.util.spec_from_file_location('cf_report', RD + '/cf_report.py')
cf_report = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(cf_report)


def _get(path, **q):
    time.sleep(0.25)                                  # paced,与选币取数节流同规
    url = '%s/%s?%s' % (API, path, urllib.parse.urlencode(q))
    return json.load(urllib.request.urlopen(url, timeout=20))


def fsym(s):
    return s.split('/')[0] + 'USDT'


def klines_to_df(sym, rows):
    """fapi kline 数组 → CANDLE_COLS(9列) DataFrame。映射与 exchanges/binance.py 同:
    vol=第5列基础量, volCcy=vol(binance.py:241), quote_volume=第7列真 qv。"""
    df = pd.DataFrame([{
        'symbol': sym,
        'candle_begin_time': pd.to_datetime(int(k[0]), unit='ms'),
        'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]),
        'close': float(k[4]), 'vol': float(k[5]), 'volCcy': float(k[5]),
        'quote_volume': float(k[7]),
    } for k in rows])
    if df.empty:
        return df
    return (df.drop_duplicates('candle_begin_time')
            .sort_values('candle_begin_time').reset_index(drop=True)[list(CANDLE_COLS)])


def funding_to_df(sym, rows):
    return pd.DataFrame([{'ts': int(k['fundingTime']), 'symbol': sym,
                          'fundingRate': float(k['fundingRate']),
                          'realizedRate': float(k['fundingRate'])} for k in rows],
                        columns=['ts', 'symbol', 'fundingRate', 'realizedRate'])


def fetch_klines(sym, interval, start_ms, end_ms):
    rows, cur = [], start_ms
    while cur < end_ms:
        ks = _get('klines', symbol=fsym(sym), interval=interval,
                  startTime=cur, endTime=end_ms, limit=1500)
        if not ks:
            break
        rows.extend(ks)
        nxt = int(ks[-1][0]) + 1
        if nxt <= cur or len(ks) < 1500:
            break
        cur = nxt
    return klines_to_df(sym, rows)


def fetch_universe():
    info = _get('exchangeInfo')
    syms = ['%s/USDT:USDT' % s['baseAsset'] for s in info['symbols']
            if s.get('contractType') == 'PERPETUAL' and s.get('quoteAsset') == 'USDT'
            and s.get('status') == 'TRADING']
    bl = set(effective_blacklist((), DEFAULT_TIER_POLICY))
    return sorted(set(syms) - bl)


def main(snaps_path, grids_path):
    snaps = json.load(open(snaps_path))
    grids = json.load(open(grids_path))
    now_ms = int(time.time() * 1000)
    rounds = [s for s in snaps if s['run_time'] + 12 * 3600 * 1000 + 300000 < now_ms]
    if not rounds:
        print('无已收盘轮', flush=True)
        return
    lo_rt = min(s['run_time'] for s in rounds)
    hi_rt = max(s['run_time'] for s in rounds)
    syms = fetch_universe()
    print('universe=%d 轮=%d' % (len(syms), len(rounds)), flush=True)
    h1_lo = lo_rt - 200 * 3600 * 1000                 # 160根max_candle_num+24h qv+余量
    series = {}
    for s_ in syms:
        df = fetch_klines(s_, '1h', h1_lo, hi_rt)
        if len(df) >= 24:
            series[s_] = df
    m1_lo = lo_rt - 27 * 3600 * 1000                  # pv 基线 (n+8)×15min=27h
    m1_hi = hi_rt + 12 * 3600 * 1000 + 60000
    m1_map, fd_map = {}, {}
    rows = []
    devnull = open(os.devnull, 'w')
    for s in rounds:
        rt = pd.to_datetime(s['run_time'], unit='ms')
        pool_c = build_pit_candidates(series, rt, max_candle_num=160,
                                      min_quote_volume=0.0,
                                      top_volume_pct=TOP_VOLUME_PCT, blacklist=())
        # 选中真值=实际开出的格(grids 按 offset+created_at≈rt 匹配;快照 picks 并读)
        picked = {g['symbol'] for g in grids
                  if int(g['offset']) == int(s['offset'])
                  and abs(int(g['created_at']) - s['run_time']) < 600000}
        snap_picks = {r['symbol'] for r in s['ranked']}
        with contextlib.redirect_stdout(devnull):
            fdf = proceed_calc_symbol_factor(
                {k: v.copy() for k, v in pool_c.items()}, rt, '12H', int(s['offset']))
        if fdf is None or fdf.empty:
            continue
        atr = dict(zip(fdf['symbol'], fdf['Atr_5']))
        for sym in sorted(set(pool_c) | picked):
            a5 = atr.get(sym)
            if a5 is None or not np.isfinite(a5):
                continue
            m1 = m1_map.get(sym)
            if m1 is None:
                m1 = fetch_klines(sym, '1m', m1_lo, m1_hi)
                m1_map[sym] = m1
            fd = fd_map.get(sym)
            if fd is None:
                fd = funding_to_df(sym, _get('fundingRate', symbol=fsym(sym),
                                             startTime=m1_lo - 86400000,
                                             endTime=m1_hi, limit=1000))
                fd_map[sym] = fd
            out = cf_eval.eval_grid(m1, fd, rt, float(a5), geometry='v2')
            if out is None:
                continue
            rows.append({'run_time': rt, 'offset': int(s['offset']), 'symbol': sym,
                         'in_pool': sym in pool_c, 'picked': sym in picked,
                         'snap_pick': sym in snap_picks, 'Atr_5': float(a5), **out})
    devnull.close()
    cf = pd.DataFrame(rows)
    tag = pd.to_datetime(lo_rt, unit='ms').strftime('%Y-%m-%d')
    cf.to_parquet('%s/ablation/cf_live_%s.parquet' % (RD, tag))
    d = cf_report.per_round_metrics(cf)
    a = cf_report.aggregate(d, cf)
    print('[%s] 轮=%d alpha_e0=%+.1fbp capture=%.2f regret=%+.1fbp 池中位=%+.1fbp '
          '税(选/池)=%+.1f/%+.1fbp 池外选中=%d'
          % (tag, a['rounds'], a['alpha_e0_bp'], a['capture'], a['regret_bp'],
             a['pool_med_bp'], a['tax_picks_bp'], a['tax_pool_bp'],
             a['picks_outside_pool']), flush=True)
    # 账本对照(锚,只覆盖 s030 口径):CLOSED 格 Δ=sim−live
    dd = []
    for g in grids:
        m = cf[(cf['picked']) & (cf['symbol'] == g['symbol'])
               & (cf['offset'] == int(g['offset']))
               & (abs(cf['run_time'].astype('int64') // 10**6
                      - int(g['created_at'])) < 600000)]
        if len(m) == 1 and g.get('pnl_ratio') is not None:
            dd.append({'symbol': g['symbol'], 'offset': int(g['offset']),
                       'sim': float(m['pnl_s030'].iloc[0]),
                       'live': float(g['pnl_ratio']),
                       'reason_sim': m['reason_s030'].iloc[0],
                       'reason_live': g.get('close_reason', '?')})
    if dd:
        ddf = pd.DataFrame(dd)
        ddf['delta_pp'] = (ddf['sim'] - ddf['live']) * 100
        med = ddf['delta_pp'].abs().median()
        print('账本对照 n=%d |Δ|中位=%.3fpp max=%.3fpp 判线≤0.5pp: %s'
              % (len(ddf), med, ddf['delta_pp'].abs().max(),
                 'PASS' if med <= 0.5 else 'FAIL'), flush=True)
        print(ddf.to_string(index=False), flush=True)
    else:
        print('账本对照: 无可匹配 CLOSED 格', flush=True)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
