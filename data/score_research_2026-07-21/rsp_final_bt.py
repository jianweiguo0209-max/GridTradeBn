"""RSP111 组合战役(2026-07-25 用户令,brief docs/superpowers/specs/2026-07-25-rsp-battle-brief.md)。

**选币器 RSP111**(冻结,禁调):
  rs = rank(Reg_v2_5, asc) + rank(Sgcz_5, asc) + rank(p12_cross1, desc)，等权，method='first'
  每轮取 rs 最小者。候选集与 p12 臂同口径(top55% 池 ∩ 有 p12 标签 ∩ 布网列非空)。

**因子来源=面板 join**(用户令 2026-07-25"先用面板数据跑,第一轮效率第一"):
Reg_v2_5/Sgcz_5 取自 sc_factors_*/hold_factors_* 面板,按 (rt,symbol,offset) 对齐;
判定八窗**直接复用上一战 p12_pool_*.parquet**(同口径:select_grid_coin 之前的过滤前
全池),省去重跑 POOL 的 ~2h。
  前置验证(rsp_panel_check.py / rsp_top1_check.py):面板与回放的 Reg_v2_5 **完全一致**、
  Sgcz_5 差 ≤3.3e-16(float64 1 ulp,求和顺序差异);HOLD-A 抽样 **60/60 轮 RSP111
  top-1 完全一致**。残余风险:rank(method='first') 对并列敏感,ulp 级差异理论上可翻转
  排名(抽样 0.5%,未见)。若终裁结果贴线,须以重跑 POOL 同源复核后再定。

**臂**(六臂;pv 均 mult5/n100/thr−1%、trailing 均 k0.15/floor1%,除 s030 用生产现值全套):
  anchor    rank_sum × s030(锚,必须逐位复现上一战补全后基准)
  v2固3     stop 0.03            St4  stop 0.04            St5 stop 0.05
  F30       stop 0.05 + funding 0.003                      s030 生产现值全套(分离选币/链贡献)
F99 不进本战役(carry 定性,brief §4)。

用法: BT_STAGE=POOL|MAIN|HOLD-E [BT_WINDOWS=W1,W2] rsp_final_bt.py
"""
import contextlib
import glob
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
from gridtrade.backtest.backtest_run import (BT_FACTORS, BT_STRATEGY, BT_UNIVERSE_TOP_PCT,
                                             _FUNDING_BACK_MS, allocate_with_tiers,
                                             holding_bars)
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.selection_replay import build_pit_candidates
from gridtrade.backtest.shock_replay import blocked_rts
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.grid_params import GRID_ROW_FACTORS
from gridtrade.core.selection import (compute_offset, needed_factors,
                                      proceed_calc_symbol_factor, select_grid_coin)
from gridtrade.core.tier_policy import effective_blacklist

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
OUT = RD + '/ablation'
RESULTS = OUT + '/rsp_final_results.txt'
STAGE = os.environ.get('BT_STAGE', 'MAIN')

# 判定八窗(brief §3.2)——**全部污染**,只做估计不做裁决
WD_MAIN = {'W1': ('2025-08-15', '2025-10-14'), 'W2': ('2025-10-15', '2025-12-14'),
           'OOS': ('2026-01-01', '2026-02-28'), 'IS': ('2026-03-01', '2026-06-30'),
           'HOLD-A': ('2025-02-01', '2025-03-31'), 'HOLD-B': ('2024-10-01', '2024-11-30'),
           'HOLD-C': ('2025-04-01', '2025-05-31'), 'HOLD-D': ('2024-12-01', '2025-01-31')}
# 唯一裁决窗(全库唯一未触碰近代时段;数据构建须在预注册 commit 之后)
WD_HOLDE = {'HOLD-E': ('2025-06-01', '2025-08-14')}
WD_ALL = dict(WD_MAIN, **WD_HOLDE)
WD = WD_HOLDE if STAGE == 'HOLD-E' else (WD_ALL if STAGE == 'POOL' else WD_MAIN)
_only = os.environ.get('BT_WINDOWS', '')
if _only:
    WD = {k: v for k, v in WD.items() if k in _only.split(',')}

# 锚基准 = 上一战**档案补全后**的八窗读数(a340353)。本战役档案与代码路径同源,
# 故要求**逐位复现**(brief §4 锚纪律:不复现即停手)。
BASE_ANCHOR = {'W1': (-2.86, -3.7), 'W2': (6.31, 17.4), 'OOS': (2.06, 5.2),
               'IS': (13.11, 11.2), 'HOLD-A': (-2.33, -2.9), 'HOLD-B': (1.75, 4.5),
               'HOLD-C': (-2.69, -2.9), 'HOLD-D': (-2.46, -3.0)}
ANCHOR_MODE = os.environ.get('RSP_ANCHOR_MODE', 'strict')

LAB = {w: '%s/hold_labels_%s.parquet' % (OUT, w) for w in WD_ALL}
# 因子面板(Reg_v2_5/Sgcz_5 来源;按 (rt,symbol,offset) join)
PANEL = {w: '%s/sc_factors_%s.parquet' % (RD, w) for w in ('W1', 'W2', 'OOS', 'IS')}
PANEL.update({w: '%s/hold_factors_%s.parquet' % (OUT, w)
              for w in ('HOLD-A', 'HOLD-B', 'HOLD-C', 'HOLD-D', 'HOLD-E')})


def pool_path(wn):
    """判定八窗复用上一战 POOL(口径同:过滤前全池);HOLD-E 本战新建。"""
    p12 = '%s/p12_pool_%s.parquet' % (OUT, wn)
    if wn in WD_MAIN and os.path.exists(p12):
        return p12
    return '%s/rsp_pool_%s.parquet' % (OUT, wn)

# 链(基线=生产现值 s030;overrides 为净差异,冻结禁调)
TR = {'trailing_k': 0.15, 'trailing_floor': 0.01, 'pv_mult': 5}
ARMS = [('anchor', 'rank', {}),
        ('rsp_v2f3', 'rsp', dict(TR)),                                  # stop 0.03(=基线)
        ('rsp_St4', 'rsp', dict(TR, stop_loss=0.04)),
        ('rsp_St5', 'rsp', dict(TR, stop_loss=0.05)),
        ('rsp_F30', 'rsp', dict(TR, stop_loss=0.05, funding_stop=0.003)),
        ('rsp_s030', 'rsp', {})]                                        # 生产现值全套
RSP_ARMS = [a[0] for a in ARMS if a[1] == 'rsp']

# 与 p12_pool_* 同结构(Reg/Sgcz 走面板 join,不入 POOL 表)⇒ 判定八窗可直接复用上一战产物
POOL_COLS = ['rt', 'offset', 'symbol', 'rank', 'close', 'Atr_5', 'middle_5']
RCOLS = ['Reg_v2_5', 'Sgcz_5']
CKPT_EVERY = int(os.environ.get('RSP_CKPT_EVERY', '100'))


def emit(line):
    open(RESULTS, 'a').write(line + '\n')
    print(line, flush=True)


# ---------------- 阶段 POOL(轮级断点续跑) ----------------
def _ckpt_paths(wn):
    return ('%s/rsp_pool_%s.ckdone.txt' % (OUT, wn), '%s/rsp_pool_%s.ckpart' % (OUT, wn))


def _load_ckpt(wn):
    done_p, part_pre = _ckpt_paths(wn)
    rows, done = [], set()
    if os.path.exists(done_p):
        done = {ln.strip() for ln in open(done_p, encoding='utf-8') if ln.strip()}
    for p in sorted(glob.glob(part_pre + '*.parquet')):
        rows.append(pd.read_parquet(p))
    return (pd.concat(rows, ignore_index=True) if rows else None), done


def build_pool(cache, wn, s0, e0):
    out_p = '%s/rsp_pool_%s.parquet' % (OUT, wn)
    if os.path.exists(out_p):
        emit('[POOL/%s] SKIP(已有)' % wn)
        return
    done_p, part_pre = _ckpt_paths(wn)
    prev, done = _load_ckpt(wn)
    if done:
        emit('[POOL/%s] RESUME 已完成轮=%d 已存行=%d'
             % (wn, len(done), 0 if prev is None else len(prev)))
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    syms = sorted(set(V.list_archive_symbols()) - set(bl))
    ws, we = pd.Timestamp(s0), pd.Timestamp(e0) + pd.Timedelta(days=1)
    rts_all = [pd.Timestamp(t) for t in pd.date_range(ws, we, freq='1H')]
    rts = [rt for rt in rts_all if str(rt) not in done]
    if rts:
        t0 = time.time()
        series = SR.load_full_series(cache, syms, '1h')
        lo = ws - pd.Timedelta(days=10)
        for s_ in list(series):
            df = series[s_]
            df = df[(df['candle_begin_time'] >= lo) & (df['candle_begin_time'] < we)]
            if len(df) < 24:
                del series[s_]
            else:
                series[s_] = df.reset_index(drop=True)
        emit('[POOL/%s] 载1h %.1fmin 有效币=%d 待跑轮=%d/%d'
             % (wn, (time.time() - t0) / 60, len(series), len(rts), len(rts_all)))
        st = dict(BT_STRATEGY, choose_symbols=9999)
        state = {'buf': [], 'pend': [],
                 'part': len(glob.glob(part_pre + '*.parquet')), 'n': 0}
        t0 = time.time()

        def flush():
            if state['buf']:
                pd.DataFrame(state['buf'], columns=POOL_COLS).to_parquet(
                    '%s%04d.parquet' % (part_pre, state['part']))
                state['part'] += 1
                state['buf'] = []
            if state['pend']:
                with open(done_p, 'a', encoding='utf-8') as f:
                    f.write('\n'.join(state['pend']) + '\n')
                state['pend'] = []

        # 与 p12_final_bt.build_pool 同构,仅多存 RCOLS 两列:
        # 在 select_grid_coin **之前**截获全池(过滤 v1.0/选币因子 dropna 不施于 RSP);
        # rank 仍由 select_grid_coin 原样产出(锚臂 byte-exact 不受影响)。
        needed = needed_factors(BT_FACTORS) | set(GRID_ROW_FACTORS) | set(RCOLS)
        devnull = open(os.devnull, 'w')
        try:
            for rt in rts:
                rt = pd.Timestamp(rt)
                off = compute_offset(rt, st['period'])
                cand = build_pit_candidates(
                    series, rt, max_candle_num=st['max_candle_num'],
                    min_quote_volume=0.0, top_volume_pct=BT_UNIVERSE_TOP_PCT, blacklist=bl)
                if cand:
                    with contextlib.redirect_stdout(devnull):
                        all_df = proceed_calc_symbol_factor(cand, rt, st['period'], off,
                                                            needed=needed, batch=True)
                        ranked = None
                        if all_df is not None and not all_df.empty:
                            ranked = select_grid_coin(all_df.copy(), BT_FACTORS,
                                                      st['weight_list'],
                                                      st['choose_symbols'], rt)
                    if all_df is not None and not all_df.empty:
                        rk = {}
                        if ranked is not None and not ranked.empty:
                            keep = ranked[(ranked['time']
                                           + pd.to_timedelta(st['period'])) >= rt]
                            rk = dict(zip(keep['symbol'], keep['rank']))
                        valid = all_df[(all_df['time']
                                        + pd.to_timedelta(st['period'])) >= rt]
                        for r in valid.itertuples(index=False):
                            state['buf'].append(
                                (rt, int(off), r.symbol,
                                 float(rk.get(r.symbol, float('nan'))),
                                 float(r.close), float(r.Atr_5), float(r.middle_5)))
                state['pend'].append(str(rt))
                state['n'] += 1
                if state['n'] % CKPT_EVERY == 0:
                    flush()
                    emit('[POOL/%s] ckpt 轮=%d/%d %.1fmin'
                         % (wn, state['n'], len(rts), (time.time() - t0) / 60))
        finally:
            devnull.close()
        flush()
    df, _ = _load_ckpt(wn)
    if df is None or df.empty:
        emit('[POOL/%s] !!! 无候选行' % wn)
        return
    df = df.sort_values(['rt', 'rank']).reset_index(drop=True)
    df.to_parquet(out_p)
    for p in glob.glob(part_pre + '*.parquet'):
        os.remove(p)
    if os.path.exists(done_p):
        os.remove(done_p)
    emit('[POOL/%s] DONE 行=%d 轮=%d 币=%d 候选/轮=%.0f 有rank=%d'
         % (wn, len(df), df['rt'].nunique(), df['symbol'].nunique(),
            len(df) / max(df['rt'].nunique(), 1), int(df['rank'].notna().sum())))


# ---------------- 建臂 picks ----------------
def _load_p12(wn):
    lab = pd.read_parquet(LAB[wn])[['rt', 'symbol', 'cross1']]
    lab = lab.rename(columns={'cross1': 'p12'})
    lab['rt'] = lab['rt'] + pd.Timedelta(hours=12)   # 标签 rt=T 描述[T,T+12h) → 轮 R=T+12h
    return lab


def make_picks(pool, ranker, wn, k=SW.TIER_CAND_K):
    """全池候选 → 按 ranker 取 top-k 候选(喂 allocate_with_tiers 做 cap2 递补)。

    anchor: rank 非空且 <=k(生产名次,逐位同上一战)
    rsp   : RSP111 三 rank 等权和取最小;候选集与 p12 臂同口径
            (布网列非空 ∩ 有 p12 标签),另需 Reg/Sgcz 非空(排名输入,来自面板 join)
    """
    d = pool
    if ranker == 'rsp':
        d = d[np.isfinite(d['close']) & np.isfinite(d['Atr_5'])
              & np.isfinite(d['middle_5'])]
        d = d.merge(_load_p12(wn), on=['rt', 'symbol'], how='inner')
        pn = pd.read_parquet(PANEL[wn])[['rt', 'symbol', 'offset'] + RCOLS]
        d = d.merge(pn, on=['rt', 'symbol', 'offset'], how='inner')
        d = d[np.isfinite(d['Reg_v2_5']) & np.isfinite(d['Sgcz_5'])]
        g = d.groupby('rt', sort=False)
        rs = (g['Reg_v2_5'].rank(method='first', ascending=True)
              + g['Sgcz_5'].rank(method='first', ascending=True)
              + g['p12'].rank(method='first', ascending=False))
        d = d.assign(_rs=rs).sort_values(['rt', '_rs', 'symbol'])
        d = d.groupby('rt', sort=False).head(k).copy()
        d['rank'] = d.groupby('rt', sort=False).cumcount() + 1.0
    else:
        d = d[np.isfinite(d['rank']) & (d['rank'] <= k)].sort_values(['rt', 'rank'])
    picks = []
    for r in d.itertuples(index=False):
        row = pd.Series({'symbol': r.symbol, 'rank': r.rank, 'close': r.close,
                         'Atr_5': r.Atr_5, 'middle_5': r.middle_5, 'time': r.rt})
        picks.append((r.rt, int(r.offset), row))
    return picks


def preload_from_picks(cache, picks, wn, s0, e0, universe):
    """shock 剔轮 → cap2 递补 → 载 1m → 切 bars/funding(生产现值,不动)。"""
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
    emit('== rsp_final %s 开跑 %s ==' % (STAGE, time.strftime('%m-%d %H:%M')))
    if STAGE == 'POOL':
        for wn, (s0, e0) in WD.items():
            build_pool(cache, wn, s0, e0)
        emit('RSP_POOL_DONE')
        return
    done = _done()
    tag = 'HOLD-E' if STAGE == 'HOLD-E' else 'MAIN'
    for wn, (s0, e0) in WD.items():
        todo = [a for a in ARMS if (wn, a[0]) not in done]
        if not todo:
            emit('[%s] SKIP' % wn)
            continue
        pool_p = pool_path(wn)
        if not os.path.exists(pool_p):
            emit('[%s] 缺候选表 —— 先跑 BT_STAGE=POOL' % wn)
            continue
        pool = pd.read_parquet(pool_p)
        n_anchor = None
        for ranker in ('rank', 'rsp'):     # 同选币器臂共享 wd 与 pv_cache
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
                df = SW.run_arm(wd, SW.Arm('rsp', name, ov), pv_cache, workers=w_sim)
                m = SW.metrics(df, wd.days)
                er = Counter(df['exit_reason'])
                top = ', '.join('%s:%d' % (k[:4], v) for k, v in er.most_common(4))
                emit('%s/%s: %-9s ret%+7.2f mdd%6.2f calmar%7.1f 格%d 破%d 爆%d 固%d pv%d '
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
                            emit('  !!! 锚不复现(%+.2f/%.1f vs %+.2f/%.1f)——停手查保真度'
                                 % (m['ret'] * 100, m['calmar'], br, bc))
                            return
                        emit('  [锚校验] %s (%+.2f/%.1f vs 存档 %+.2f/%.1f)'
                             % ('OK 逐位复现' if not dev else '偏差(record模式)',
                                m['ret'] * 100, m['calmar'], br, bc))
                    else:
                        emit('  [锚结构自检] 格=%d offset=%d 破%d 爆%d'
                             % (m['n_grids'], df['offset'].nunique(),
                                m['n_broke'], m['n_blown']))
                elif n_anchor:
                    r = m['n_grids'] / n_anchor
                    if r < 0.95:
                        emit('  ⚠ %s 有效格数 %d = 锚的 %.1f%% < 95%%'
                             % (name, m['n_grids'], r * 100))
        emit('[%s] DONE' % wn)
    emit('RSP_%s_DONE' % STAGE)


if __name__ == '__main__':
    main()
