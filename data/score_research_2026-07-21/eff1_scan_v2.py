"""T1 重跑:九窗 × 31 臂,**票池剔除 spacing < MIN_TICKS×tick 的币**(2026-07-26 用户令)。

**为什么重跑**:原扫描(eff1_scan.py,279 读数)在 OOS/IS 两窗被 tick 盲区严重污染
——同臂剔粗 tick 币后 OOS +20.79→+5.28(留存 25.4%)、IS +24.22→+5.81(24.0%),
而污染度 0 的 IS-seg2 留存 **100.0%**(见 tick-blindspot-is-eff1-edge)。
原扫描的处置是「剔除污染窗」;本次改为「清洗票池后让九窗全部参与」,更严谨。

**与 eff1_scan.py 的三处差异**(其余逐位照抄,ARMS/WD9/make_picks 直接复用):
  ① **逐几何过滤**:tick 过滤依赖 spacing ⇒ picks 随几何变 ⇒ P1 层不能再九臂共享一个 wd,
     改按 (variant, 几何) 分组。P3/P5 用基线几何,与 b3_c16 同组。
  ② **窗内共享 1m 序列**:各几何的 picks 大部分重合,每窗只读一次序列、逐几何组装 wd
     并即时释放 ⇒ 内存峰值仍是「1 份序列 + 1 个 wd」,但省掉 8 次重读。
  ③ **瘦身加载**:read_days_range 只读 [s0-3d, e0+2d] 而非 read_all_days 读全史。
     ⚠ 前置 3 天不可省——wd.raw 第 6 元素是整条 series,pv 尖峰要 27h 前置历史;
     只从 s0 读会让窗口开头那批格 pv 信号不同(实错:b2.5_c16 +56.15 vs 存档 +56.09,
     而 b2_c26 因后段复利主导"巧合"对上、更险)。已在 OOS 三臂验证逐位等价。

**字节门**:`MIN_TICKS=0` 时过滤关闭,任一窗跑满 31 臂必须逐位复现 eff1_scan_results.txt。
三处改动同时动了分组/共享/加载,任何一处偏差都会让 279 个读数全废,此门省不得。

用法: [MIN_TICKS=3] [BT_WINDOWS=OOS] [BT_WORKERS=4] eff1_scan_v2.py
"""
import importlib.util
import os
import sys
import time
from collections import Counter

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import (BT_UNIVERSE_TOP_PCT, _FUNDING_BACK_MS,
                                             allocate_with_tiers, holding_bars)
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import CANDLE_COLS
from gridtrade.backtest.shock_replay import blocked_rts
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.grid_params import calc_grid_params_v2
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
_s = importlib.util.spec_from_file_location('sc', RD + '/eff1_scan.py')
S = importlib.util.module_from_spec(_s)
_s.loader.exec_module(S)
_g = importlib.util.spec_from_file_location('tg', RD + '/tick_gate_ninewin.py')
G = importlib.util.module_from_spec(_g)
_g.loader.exec_module(G)

MIN_TICKS = float(os.environ.get('MIN_TICKS', '3'))
RESULTS = RD + '/ablation/eff1_scan_v2_results.txt'
WD = dict(S.WD9)
for _bad in ('HOLD-F', 'JUL26'):          # 终审专属,任何扫描变体禁入
    WD.pop(_bad, None)
_only = [w for w in os.environ.get('BT_WINDOWS', '').split(',') if w]
if _only:
    WD = {w: v for w, v in WD.items() if w in _only}


def emit(line):
    open(RESULTS, 'a').write(line + '\n')
    print(line, flush=True)


def tick_filter(picks, tk, geo):
    """剔除 spacing < MIN_TICKS×tick 的候选;缺 tick → fail-open 保留。
    剩下的交给 allocate_with_tiers 原有 pick_first_allowed 递补(机制现成)。"""
    if MIN_TICKS <= 0:
        return picks, 0
    v2 = dict(SW._V2, atr_range_multiplier=geo['band'], grid_count_min=geo['count_min'],
              grid_spacing_max=SW.baseline()['spacing_max'])
    out, drop = [], 0
    for rt, off, row in picks:
        m = tk.get(row['symbol'])
        t = m.get(pd.Timestamp(rt).date()) if m else None
        if not t or t != t:
            out.append((rt, off, row))
            continue
        try:
            p = calc_grid_params_v2(row=row, price_limit=SW._S['price_limit'],
                                    stop_limit=SW._S['stop_limit'], v2_config=v2)
        except Exception:
            out.append((rt, off, row))
            continue
        if (p['high_price'] - p['low_price']) / p['grid_count'] >= MIN_TICKS * t:
            out.append((rt, off, row))
        else:
            drop += 1
    return out, drop


def preload(cache, picks, wn, s0, e0, blocked, series):
    """series 是**窗内共享**的 dict,按需补读(逐几何复用,省 8 次重读)。"""
    picks = [p for p in picks if p[0] not in blocked]
    picks, _ = allocate_with_tiers(picks, DEFAULT_TIER_POLICY, period=SW._S['period'])
    syms = sorted({row['symbol'] for _, _, row in picks})
    lo = str((pd.Timestamp(s0) - pd.Timedelta(days=3)).date())   # pv 要 27h 前置,勿省
    hi = str((pd.Timestamp(e0) + pd.Timedelta(days=2)).date())
    for s in syms:
        if s in series:
            continue
        df = cache.read_days_range('1m', s, lo, hi)
        if df is None or df.empty:
            continue
        df = df[CANDLE_COLS].copy()
        df.sort_values('candle_begin_time', inplace=True)
        df.drop_duplicates(subset=['candle_begin_time'], keep='last', inplace=True)
        series[s] = df.reset_index(drop=True)
    fmap, raw = {}, []
    for rt, offset, row in picks:
        sym = row['symbol']
        if sym not in series:
            continue
        bars = holding_bars(series[sym], rt, SW._S['period'])
        if len(bars) == 0:
            continue
        if sym not in fmap:
            fmap[sym] = cache.read_all_days('funding', sym)
        fd = fmap[sym]
        if fd is not None and not fd.empty:
            b0 = int(bars['candle_begin_time'].min().value // 1_000_000)
            b1 = int(bars['candle_begin_time'].max().value // 1_000_000)
            fd = fd[(fd['ts'] >= b0 - _FUNDING_BACK_MS) & (fd['ts'] <= b1)]
        raw.append((rt, int(offset), row, bars, fd, series[sym]))
    days = int((pd.Timestamp(e0) - pd.Timestamp(s0)).days) + 1
    return SW.WindowData(name=wn, start=pd.Timestamp(s0), end=pd.Timestamp(e0),
                         days=days, raw=raw, n_blocked=len(blocked), n_symbols=len(syms))


def _done():
    done = set()
    try:
        for ln in open(RESULTS):
            if '/' in ln and ':' in ln:
                head = ln.split(':', 1)
                if '/' in head[0]:
                    wn = head[0].split('/')[1]
                    nm = head[1].split()[0] if len(head) > 1 and head[1].split() else ''
                    if wn in WD:
                        done.add((wn, nm))
    except FileNotFoundError:
        pass
    return done


def main():
    cache = ParquetCache(V.default_cache_root())
    w_sim = int(os.environ.get('BT_WORKERS', '4'))
    universe = sorted(set(V.list_archive_symbols())
                      - set(effective_blacklist((), DEFAULT_TIER_POLICY)))
    SW.set_baseline({})
    emit('== eff1_scan_v2 MIN_TICKS=%g workers=%d 开跑 %s (%d臂×%d窗) =='
         % (MIN_TICKS, w_sim, time.strftime('%m-%d %H:%M'), len(S.ARMS), len(WD)))
    done = _done()
    for wn, (s0, e0) in WD.items():
        todo = [a for a in S.ARMS if (wn, a[0]) not in done]
        if not todo:
            emit('[%s] SKIP' % wn)
            continue
        pool = pd.read_parquet(S.pool_path(wn))
        t0 = time.time()
        blocked = blocked_rts(cache, universe, pd.Timestamp(s0),
                              pd.Timestamp(e0) + pd.Timedelta(days=1), '1h', *SW.SHOCK,
                              min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
        tk = {}
        if MIN_TICKS > 0:
            lo = str((pd.Timestamp(s0) - pd.Timedelta(days=4)).date())
            hi = str((pd.Timestamp(e0) + pd.Timedelta(days=1)).date())
            base = S.make_picks(pool, 'K1', wn)
            for sym in sorted({r['symbol'] for _, _, r in base}):
                tk[sym] = G.daily_tick(cache, sym, lo, hi)
        emit('[%s] blocked=%d tick表=%d币 %.1fmin'
             % (wn, len(blocked), len(tk), (time.time() - t0) / 60))
        series = {}                       # 窗内共享,窗末释放
        by_key = {}
        for a in todo:                    # a = (name, variant, chain_ov, geo_ov)
            geo = a[3] or {'band': SW._V2['atr_range_multiplier'],
                           'count_min': SW._V2['grid_count_min']}
            by_key.setdefault((a[1], geo['band'], geo['count_min']), []).append((a, geo))
        for (variant, _b, _c), items in by_key.items():
            geo = items[0][1]
            t0 = time.time()
            picks, drop = tick_filter(S.make_picks(pool, variant, wn), tk, geo)
            wd = preload(cache, picks, wn, s0, e0, blocked, series)
            emit('[%s] %s b%g_c%d preload %.1fmin 格=%d 剔候选=%d'
                 % (wn, variant, geo['band'], geo['count_min'],
                    (time.time() - t0) / 60, len(wd.raw), drop))
            pv_cache = {}
            for (name, _v, chain_ov, geo_ov), _g2 in items:
                t0 = time.time()
                ov = dict(chain_ov)
                ov.update(geo_ov)
                df = SW.run_arm(wd, SW.Arm('eff1', name, ov), pv_cache, workers=w_sim)
                m = SW.metrics(df, wd.days)
                er = Counter(df['exit_reason'])
                top = ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(3))
                nf = float(df['n_fills'].mean())
                emit('%s/%s: %-12s ret%+9.2f mdd%6.2f calmar%10.4g fills%7.1f[%s] '
                     '格%d 破%d 爆%d %.1fmin | %s'
                     % (S.ARM_LAYER.get(name, 'P?'), wn, name, m['ret'] * 100,
                        -m['mdd'] * 100, m['calmar'], nf, S.fill_trust(nf),
                        m['n_grids'], m['n_broke'], m['n_blown'],
                        (time.time() - t0) / 60, top))
            del wd
        del series
        emit('[%s] DONE' % wn)
    emit('EFF1_SCAN_V2_DONE MIN_TICKS=%g' % MIN_TICKS)


if __name__ == '__main__':
    main()
