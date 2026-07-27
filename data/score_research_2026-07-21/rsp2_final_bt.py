"""消融格组合战役(2026-07-26,brief 修订三 docs/superpowers/specs/2026-07-25-rsp-battle-brief.md)。

**待验 = {D_ESP, D_REP, EP2} × {s030, v2固3, St4, St5, F30, F99} = 18 臂 + 锚**
RSP111 五链已全样本出局(修订二),不在本脚本内。

**选币器**(全部等权名次和、top-1、method='first'、冻结禁调;候选集与 p12 臂同口径:
top55% 池 ∩ 有 p12 标签 ∩ 布网列非空 ∩ 所用因子非空):
  D_ESP = rank(Er_2↑) + rank(Sgcz_5↑) + rank(p12↓)      探针 +10.9bp/t3.20
  D_REP = rank(Reg_v2_5↑) + rank(Er_2↑) + rank(p12↓)    探针 +10.5/t3.11
  EP2   = rank(Er_2↑) + rank(p12↓)  双因子裸核           探针 +9.8/t2.63
机制注记:Er_2(围绕 EMA 的均衡振荡度)是比 Reg/Sgcz 更强的燃料否决器——"振荡门控燃料";
EP2 检验第三因子是否冗余。⚠同源折扣:三者提名读数出自同一台探针机器(本周累计~57臂,
t3.2 为其中最大值),选择效应在场,预期显著收缩。

**链**(v2 族共性:trailing k0.15/floor1%、pv mult5/n100/thr−1%;s030=生产现值全套):
  s030 stop0.03(生产全套) | v2固3 0.03 | St4 0.04 | St5 0.05 | F30 0.05+funding0.003
  | F99 0.05+funding关(**保留 carry 标签**:超额若集中于深负费率窗且复苏窗回吐,
    按 carry 定性读数,不按选币边读数)

**工程**:每选币器 rank preload 一次、六链共享 wd 与 pv_cache;因子走面板 join,
判定八窗复用上一战 p12_pool_*(过滤前全池,同口径)。
用法: BT_STAGE=MAIN|HOLD-E [BT_WINDOWS=W1,W2] rsp2_final_bt.py
"""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import numpy as np
import pandas as pd

from gridtrade.backtest import selection_replay as SR
from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest.backtest_run import (BT_UNIVERSE_TOP_PCT, _FUNDING_BACK_MS,
                                             allocate_with_tiers, holding_bars)
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.shock_replay import blocked_rts
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
OUT = RD + '/ablation'
RESULTS = OUT + '/rsp2_final_results.txt'
STAGE = os.environ.get('BT_STAGE', 'MAIN')

WD_MAIN = {'W1': ('2025-08-15', '2025-10-14'), 'W2': ('2025-10-15', '2025-12-14'),
           'OOS': ('2026-01-01', '2026-02-28'), 'IS': ('2026-03-01', '2026-06-30'),
           'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30'),
           'HOLD-C': ('2025-04-01', '2025-05-31'), 'HOLD-D': ('2024-12-01', '2025-01-31')}
WD_HOLDE = {'HOLD-E': ('2025-06-01', '2025-08-14')}
WD_ALL = dict(WD_MAIN, **WD_HOLDE)
WD = WD_HOLDE if STAGE == 'HOLD-E' else WD_MAIN
_only = os.environ.get('BT_WINDOWS', '')
if _only:
    WD = {k: v for k, v in WD.items() if k in _only.split(',')}

# 锚基准 = 上一战补全后八窗读数;要求逐位复现(brief §4 锚纪律)
BASE_ANCHOR = {'W1': (-2.86, -3.7), 'W2': (6.31, 17.4), 'OOS': (2.06, 5.2),
               'IS': (13.11, 11.2), 'HOLD-A': (-2.33, -2.9), 'HOLD-B': (1.75, 4.5),
               'HOLD-C': (-2.69, -2.9), 'HOLD-D': (-2.46, -3.0)}
ANCHOR_MODE = os.environ.get('RSP2_ANCHOR_MODE', 'strict')

LAB = {w: '%s/hold_labels_%s.parquet' % (OUT, w) for w in WD_ALL}
PANEL = {w: '%s/sc_factors_%s.parquet' % (RD, w) for w in ('W1', 'W2', 'OOS', 'IS')}
PANEL.update({w: '%s/hold_factors_%s.parquet' % (OUT, w)
              for w in ('HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D', 'HOLD-E')})
RCOLS = ['Reg_v2_5', 'Sgcz_5', 'Er_2']       # 面板 join 的因子列(含新增 Er_2)


def pool_path(wn):
    p12 = '%s/p12_pool_%s.parquet' % (OUT, wn)
    if wn in WD_MAIN and os.path.exists(p12):
        return p12                            # 判定八窗复用上一战 POOL(过滤前全池)
    return '%s/rsp_pool_%s.parquet' % (OUT, wn)


# 选币器:(列名, ascending) 列表 —— 等权名次和,取最小(单因子即纯排序)
RANKERS = {
    'D_ESP': [('Er_2', True), ('Sgcz_5', True), ('p12', False)],
    'D_REP': [('Reg_v2_5', True), ('Er_2', True), ('p12', False)],
    'EP2':   [('Er_2', True), ('p12', False)],
    # 修订四(2026-07-26)追加:
    # eff1 = p12_eff 单因子降序 top-1。探针 +13.8bp/t4.91(全项目最强,两死窗全平
    #        W1−1.3/W2+1.4、OOS+44.7)。t4.91 超多臂噪声期望上限(~3.0)为唯一例外读数。
    'eff1':  [('p12_eff', False)],
    'EPE':   [('Er_2', True), ('p12', False), ('p12_eff', False)],   # +9.2/t2.86
}
# 各选币器配的链(修订四:eff1 配全族六链;EPE 只配 s030 与 v2固3)
SEL_CHAINS = {'D_ESP': None, 'D_REP': None, 'EP2': None, 'eff1': None,
              'EPE': ['s030', 'v2f3']}      # None = 全六链
TR = {'trailing_k': 0.15, 'trailing_floor': 0.01, 'pv_mult': 5}
CHAINS = [('s030', {}),                                          # 生产现值全套
          ('v2f3', dict(TR)),                                    # stop 0.03(=基线)
          ('St4', dict(TR, stop_loss=0.04)),
          ('St5', dict(TR, stop_loss=0.05)),
          ('F30', dict(TR, stop_loss=0.05, funding_stop=0.003)),
          ('F99', dict(TR, stop_loss=0.05, funding_stop=1.0))]    # carry 标签保留
ARMS = [('anchor', 'rank', {})] + [
    ('%s_%s' % (sel, cn), sel, dict(ov))
    for sel in RANKERS for cn, ov in CHAINS
    if SEL_CHAINS.get(sel) is None or cn in SEL_CHAINS[sel]]
CAND_ARMS = [a[0] for a in ARMS if a[1] != 'rank']


def emit(line):
    open(RESULTS, 'a').write(line + '\n')
    print(line, flush=True)


def _load_p12(wn):
    """标签表 → p12(=cross1) 与 p12_eff(=cross1/(1+100·mae),brief 修订四)。

    两者同源同 PIT:标签行 rt=T 描述 [T,T+12h),平移 +12h 对齐选币轮 R(与 p12 注入路径同)。
    eff 的机制:燃料(阶梯跨越数)除以最大不利偏移 ⇒ **单位风险的燃料**,
    正对"排除单边伪装成波动"的痛点(与 Er_2 的振荡门控互补而非重复)。
    """
    lab = pd.read_parquet(LAB[wn])[['rt', 'symbol', 'cross1', 'mae']]
    lab['p12'] = lab['cross1']
    lab['p12_eff'] = lab['cross1'] / (1.0 + 100.0 * lab['mae'])
    lab['rt'] = lab['rt'] + pd.Timedelta(hours=12)
    return lab[['rt', 'symbol', 'p12', 'p12_eff']]


def make_picks(pool, ranker, wn, k=SW.TIER_CAND_K):
    """anchor: 生产 rank;其余: 等权名次和取最小(候选集与 p12 臂同口径)。"""
    d = pool
    if ranker == 'rank':
        return _to_picks(d[np.isfinite(d['rank']) & (d['rank'] <= k)]
                         .sort_values(['rt', 'rank']))
    spec = RANKERS[ranker]
    need = [c for c, _asc in spec if c not in ('p12', 'p12_eff')]  # 标签列不在面板
    d = d[np.isfinite(d['close']) & np.isfinite(d['Atr_5']) & np.isfinite(d['middle_5'])]
    d = d.merge(_load_p12(wn), on=['rt', 'symbol'], how='inner')
    pn = pd.read_parquet(PANEL[wn])[['rt', 'symbol', 'offset'] + RCOLS]
    d = d.merge(pn, on=['rt', 'symbol', 'offset'], how='inner')
    for c in need:
        d = d[np.isfinite(d[c])]
    g = d.groupby('rt', sort=False)
    rs = None
    for c, asc in spec:
        r = g[c].rank(method='first', ascending=asc)
        rs = r if rs is None else rs + r
    d = d.assign(_rs=rs).sort_values(['rt', '_rs', 'symbol'])
    d = d.groupby('rt', sort=False).head(k).copy()
    d['rank'] = d.groupby('rt', sort=False).cumcount() + 1.0
    return _to_picks(d)


def _to_picks(d):
    picks = []
    for r in d.itertuples(index=False):
        row = pd.Series({'symbol': r.symbol, 'rank': r.rank, 'close': r.close,
                         'Atr_5': r.Atr_5, 'middle_5': r.middle_5, 'time': r.rt})
        picks.append((r.rt, int(r.offset), row))
    return picks


def preload_from_picks(cache, picks, wn, s0, e0, universe):
    ws, we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
    blocked = blocked_rts(cache, universe, ws, we, '1h', *SW.SHOCK,
                          min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT)
    picks = [p for p in picks if p[0] not in blocked]
    picks, _st = allocate_with_tiers(picks, DEFAULT_TIER_POLICY, period=SW._S['period'])
    syms = sorted({row['symbol'] for _, _, row in picks})
    series = SR.load_full_series(cache, syms, '1m')
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
            lo = int(bars['candle_begin_time'].min().value // 1_000_000)
            hi = int(bars['candle_begin_time'].max().value // 1_000_000)
            fd = fd[(fd['ts'] >= lo - _FUNDING_BACK_MS) & (fd['ts'] <= hi)]
        raw.append((rt, int(offset), row, bars, fd, series[sym]))
    days = int((pd.Timestamp(e0) - pd.Timestamp(s0)).days) + 1
    return SW.WindowData(name=wn, start=ws, end=we, days=days, raw=raw,
                         n_blocked=len(blocked), n_symbols=len(syms))


def _done():
    done = set()
    names = {a[0] for a in ARMS}
    try:
        for ln in open(RESULTS, encoding='utf-8'):
            for p in ('MAIN/', 'HOLD-E/'):
                if ln.startswith(p) and ': ' in ln:
                    wn, rest = ln[len(p):].split(': ', 1)
                    if wn in WD and rest.split()[0] in names:
                        done.add((wn, rest.split()[0]))
    except FileNotFoundError:
        pass
    return done


def main():
    cache = ParquetCache(V.default_cache_root())
    w_sim = int(os.environ.get('BT_WORKERS', '3'))
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    SW.set_baseline({})
    emit('== rsp2 %s 开跑 %s (18臂+锚) ==' % (STAGE, time.strftime('%m-%d %H:%M')))
    done = _done()
    tag = 'HOLD-E' if STAGE == 'HOLD-E' else 'MAIN'
    for wn, (s0, e0) in WD.items():
        todo = [a for a in ARMS if (wn, a[0]) not in done]
        if not todo:
            emit('[%s] SKIP' % wn)
            continue
        pool_p = pool_path(wn)
        if not os.path.exists(pool_p):
            emit('[%s] 缺候选表 %s' % (wn, pool_p))
            continue
        pool = pd.read_parquet(pool_p)
        n_anchor = None
        for ranker in ['rank'] + list(RANKERS):    # 每选币器 preload 一次,六链共享
            arms_r = [a for a in todo if a[1] == ranker]
            if not arms_r:
                continue
            t0 = time.time()
            picks = make_picks(pool, ranker, wn)
            wd = preload_from_picks(cache, picks, wn, s0, e0, universe)
            emit('[%s] %s preload %.1fmin 格=%d 币=%d blocked=%d'
                 % (wn, ranker, (time.time() - t0) / 60, len(wd.raw),
                    wd.n_symbols, wd.n_blocked))
            pv_cache = {}
            for name, _rk, ov in arms_r:
                t0 = time.time()
                df = SW.run_arm(wd, SW.Arm('rsp2', name, ov), pv_cache, workers=w_sim)
                m = SW.metrics(df, wd.days)
                er = Counter(df['exit_reason'])
                top = ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(4))
                emit('%s/%s: %-12s ret%+7.2f mdd%6.2f calmar%7.1f 格%d 破%d 爆%d 固%d pv%d '
                     '最差%+.3f %.1fmin | %s'
                     % (tag, wn, name, m['ret'] * 100, -m['mdd'] * 100, m['calmar'],
                        m['n_grids'], m['n_broke'], m['n_blown'], m['n_fixstop'],
                        m['n_pvstop'], m['worst_grid'], (time.time() - t0) / 60, top))
                if name == 'anchor':
                    n_anchor = m['n_grids']
                    if wn in BASE_ANCHOR:
                        br, bc = BASE_ANCHOR[wn]
                        dev = (abs(m['ret'] * 100 - br) > 0.02
                               or abs(m['calmar'] - bc) > 0.15)
                        if dev and ANCHOR_MODE == 'strict':
                            emit('  !!! 锚不复现(%+.2f/%.1f vs %+.2f/%.1f)——停手'
                                 % (m['ret'] * 100, m['calmar'], br, bc))
                            return
                        emit('  [锚校验] %s (%+.2f/%.1f)'
                             % ('OK 逐位' if not dev else '偏差', m['ret'] * 100, m['calmar']))
                    else:
                        emit('  [锚结构自检] 格=%d offset=%d 破%d 爆%d'
                             % (m['n_grids'], df['offset'].nunique(),
                                m['n_broke'], m['n_blown']))
                elif n_anchor and m['n_grids'] / n_anchor < 0.95:
                    emit('  ⚠ %s 有效格数 %d = 锚的 %.1f%% < 95%%'
                         % (name, m['n_grids'], m['n_grids'] / n_anchor * 100))
        emit('[%s] DONE' % wn)
    emit('RSP2_%s_DONE' % STAGE)


if __name__ == '__main__':
    main()
